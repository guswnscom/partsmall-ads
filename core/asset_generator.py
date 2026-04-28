"""Asset Generator — creates AI poster images for each ad variant.

Pipeline per variant:
  1. Build a visual prompt from variant.angle + campaign brands + branch context
  2. Call OpenAI gpt-image-1 (or DALL-E 3) to generate a clean 1024x1024 base
     (prompt explicitly tells model "no text, no logos" — we add those in post)
  3. Overlay via Pillow: PARTS-MALL logo (top-left), gradient for readability,
     headline (large bold center-bottom), WhatsApp CTA pill, branch line
  4. Save final 1080x1080 PNG to /opt/partsmall/generated/
  5. Update ad_creatives.asset_path

비용 (OpenAI gpt-image-1 medium quality, 1024x1024):
  - 변형 1개: 약 $0.04 (~R0.75)
  - 변형 5개 한 번 생성: $0.20 (~R3.75)
  - 매니저가 버튼 클릭한 경우에만 생성 (자동 X)
"""

from __future__ import annotations
import base64
import io
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .db import db

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "assets"
GENERATED_DIR = ROOT / "generated"
REF_IMAGES_DIR = ROOT / "reference" / "images"
GENERATED_DIR.mkdir(exist_ok=True)

# Brand → model keyword mapping for matching reference images by filename.
# When user drops e.g. `i20.jpg` or `kia_sportage.png` in reference/images/,
# we pick those up automatically when the campaign features that brand.
BRAND_MODEL_KEYWORDS: Dict[str, list[str]] = {
    "hyundai": [
        "hyundai", "i10", "i20", "i30", "i40", "tucson", "sonata", "elantra",
        "creta", "santafe", "santa-fe", "santa_fe", "kona", "venue",
        "atos", "atoz",
        "accent", "getz", "ix35", "veloster", "grand",
        "h1", "h-1", "h_1", "h100", "h-100", "h_100", "h350",
        "starex", "trajet", "matrix", "lavita", "azera", "genesis",
    ],
    "kia": [
        "kia", "picanto", "rio", "sportage", "cerato", "soul", "sorento",
        "seltos", "stonic", "spectra", "sedona", "carnival", "k2700",
        "k2500", "k2900", "optima", "pride", "carens", "venga", "pregio",
    ],
    "chevrolet": [
        "chevrolet", "chevy", "spark", "aveo", "sonic", "trax", "captiva",
        "cruze", "lumina", "utility", "optra", "lacetti",
    ],
    "suzuki": [
        "suzuki", "swift", "baleno", "celerio", "ertiga", "jimny",
        "vitara", "s-presso", "spresso", "ignis", "alto",
    ],
    "ssangyong": [
        "ssangyong", "tivoli", "korando", "rexton", "musso", "actyon", "stavic",
    ],
}

MAX_REF_IMAGES_PER_GEN = 2  # don't overload the prompt — pick top 2

# Logo file resolution — tolerant of multiple filenames so the user can drop
# any reasonably-named logo into assets/ without fiddling with extensions/case.
LOGO_PATH = ASSETS_DIR / "logo.png"  # final fallback (the plain logo)
ALLOWED_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _resolve_logo_path() -> Optional[Path]:
    """Find the best logo file in assets/.

    Priority order:
      1. assets/logo-official.{png,jpg,jpeg,webp}  (explicit OFFICIAL DISTRIBUTOR variant)
      2. assets/logo_official.* (underscore alternative)
      3. Any file in assets/ whose stem CONTAINS 'official' (case-insensitive)
      4. Any file in assets/ whose stem CONTAINS 'parts-mall' (case-insensitive),
         excluding the plain logo.png
      5. assets/logo.png (final fallback)
    """
    if not ASSETS_DIR.exists():
        return None

    # 1 + 2: explicit names with allowed extensions
    for stem in ("logo-official", "logo_official"):
        for ext in ALLOWED_LOGO_EXTS:
            p = ASSETS_DIR / f"{stem}{ext}"
            if p.exists():
                return p

    # 3: any file with "official" in its name
    for p in sorted(ASSETS_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() not in ALLOWED_LOGO_EXTS:
            continue
        if "official" in p.stem.lower():
            return p

    # 4: any "PARTS-MALL ..." file (the user may have saved with the brand name)
    for p in sorted(ASSETS_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() not in ALLOWED_LOGO_EXTS:
            continue
        if "parts-mall" in p.stem.lower() or "partsmall" in p.stem.lower():
            if p.name.lower() != "logo.png":
                return p

    # 5: final fallback
    if LOGO_PATH.exists():
        return LOGO_PATH
    return None


def _prepare_logo(target_height: int = 140) -> Optional[Image.Image]:
    """Load logo, remove the rectangular white background, add a soft white halo
    so it blends with any poster bg while staying legible.
    Returns RGBA image or None if logo missing.
    """
    logo_path = _resolve_logo_path()
    if not logo_path:
        return None
    try:
        logo = Image.open(logo_path).convert("RGBA")
    except Exception as e:
        log.warning("Logo load failed: %s", e)
        return None

    # 1. Make near-white pixels transparent (kills the rectangular bg + small inner noise)
    threshold = 245
    pixels = list(logo.getdata())
    new_pixels = []
    for r, g, b, a in pixels:
        if a < 10 or (r >= threshold and g >= threshold and b >= threshold):
            new_pixels.append((r, g, b, 0))
        else:
            new_pixels.append((r, g, b, a))
    logo.putdata(new_pixels)

    # 2. Resize to target height keeping aspect ratio
    ratio = target_height / logo.height
    logo = logo.resize(
        (int(logo.width * ratio), target_height), Image.LANCZOS
    )

    # 3. Build a soft white halo behind the logo silhouette for legibility on any bg
    pad = 24
    canvas_size = (logo.width + pad * 2, logo.height + pad * 2)
    alpha = logo.split()[3]

    # Halo: blur the alpha, boost it, fill with white
    halo_alpha = alpha
    # Place alpha onto larger canvas at offset (pad, pad)
    halo_alpha_canvas = Image.new("L", canvas_size, 0)
    halo_alpha_canvas.paste(halo_alpha, (pad, pad))
    halo_alpha_canvas = halo_alpha_canvas.filter(
        ImageFilter.GaussianBlur(radius=10)
    )
    # Boost intensity so the halo is visible but soft
    halo_alpha_canvas = halo_alpha_canvas.point(
        lambda v: min(255, int(v * 2.2))
    )

    halo = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
    halo.putalpha(halo_alpha_canvas)

    # Composite: halo + logo on top, both in canvas coordinates
    final = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    final.alpha_composite(halo, (0, 0))
    final.alpha_composite(logo, (pad, pad))
    return final

DEFAULT_MODEL = "gpt-image-1"
# 'high' costs ~$0.17 vs 'medium' $0.04 — but the AI-tell drops dramatically.
# We default to high for ad-quality output. Override via OPENAI_IMAGE_QUALITY env.
DEFAULT_QUALITY = "high"
FALLBACK_DALLE_QUALITY = "hd"  # dall-e-3: standard/hd

# Brand colors
NAVY = (30, 58, 138)        # #1e3a8a
EMERALD = (22, 163, 74)     # #16a34a
WHATSAPP_GREEN = (37, 211, 102)  # #25D366
WHITE = (255, 255, 255)

# Per-angle visual scene descriptors. Documentary / studio photography style.
# Critical: every scene assumes a STRICT "no text, no fake brand graphics on
# boxes/products" rule enforced by the prompt builder below.
ANGLE_SCENES: Dict[str, str] = {
    "stock": (
        "documentary photography of a clean modern automotive parts warehouse interior. "
        "Industrial steel shelving stacked with PLAIN solid navy-blue and dark-teal "
        "cardboard boxes of varying sizes (no printed graphics or labels on the boxes). "
        "Selective focus showing rows of boxes receding into soft depth-of-field, "
        "natural overhead warehouse lighting"
    ),
    "trust": (
        "studio product photography of a premium automotive brake disc set with brake pads "
        "arranged on a matte dark-grey surface. Dramatic soft directional lighting from one side, "
        "polished metal surfaces with realistic micro-reflections, shallow depth of field, "
        "magazine-editorial automotive product photography"
    ),
    "urgency": (
        "documentary photography of a clean parts counter, a single PLAIN solid-color "
        "cardboard parts box (no markings) being lifted by a mechanic's hand, shallow depth "
        "of field, slight motion, real workshop environment"
    ),
    "service": (
        "documentary photography of a skilled mechanic's hands installing a clean automotive "
        "part (such as a brake pad or filter) on a vehicle in a bright modern service bay. "
        "Blue mechanic overalls, real tools on the workbench, natural daylight from the bay door"
    ),
    "value": (
        "studio-style product photography of an arrangement of automotive replacement parts — "
        "a brake disc, an air filter, a brake pad set, and a couple of plain solid-color "
        "automotive parts boxes (no printed labels) — on a workshop floor with neutral lighting"
    ),
    "safety": (
        "documentary photography of a clean Korean-style hatchback or sedan (no badges visible) "
        "parked under bright South African daylight near a workshop, hood slightly raised showing "
        "well-maintained engine bay, sense of reliability"
    ),
    "local": (
        "documentary photography of a friendly automotive parts shop counter scene. Two adults in "
        "casual workshop attire (one customer, one staff) speaking warmly across the counter, "
        "behind them clean shelving with PLAIN solid-color cardboard parts boxes (no printed text "
        "or logos), natural shop lighting"
    ),
}


def is_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _find_reference_vehicle_images(
    brands: list[str], max_n: int = MAX_REF_IMAGES_PER_GEN
) -> list[Path]:
    """Pick reference images from reference/images/ that match the campaign's brands.

    Matches filenames against BRAND_MODEL_KEYWORDS. Returns up to max_n paths,
    randomized so different generations get different reference angles.
    """
    if not REF_IMAGES_DIR.exists():
        return []

    keywords: set[str] = set()
    for b in brands:
        keywords.update(BRAND_MODEL_KEYWORDS.get(b.lower(), [b.lower()]))

    # Recursive: also pick up files inside language-named subfolders
    # (e.g. user organised by Korean folder names like 차량사진/, 광고이미지/)
    all_imgs: list[Path] = []
    for p in sorted(REF_IMAGES_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            all_imgs.append(p)

    # Match by stem keywords against brands
    matches: list[Path] = []
    for p in all_imgs:
        name = p.stem.lower().replace(" ", "").replace("_", "").replace("-", "")
        kw_norm = [k.replace(" ", "").replace("_", "").replace("-", "") for k in keywords]
        if any(k and k in name for k in kw_norm):
            matches.append(p)

    if not matches:
        # Fallback: any image gives the model some brand visual context
        matches = all_imgs

    import random
    random.shuffle(matches)
    return matches[:max_n]


def build_visual_prompt(
    angle: str,
    brands: list[str],
    branch_city: str,
    has_references: bool = False,
) -> str:
    angle_key = (angle or "local").lower()
    scene = ANGLE_SCENES.get(angle_key, ANGLE_SCENES["local"])
    brands_str = ", ".join(brands[:3]) if brands else "Korean automotive"

    refs_block = ""
    if has_references:
        refs_block = """

REFERENCE IMAGES (provided alongside this prompt):
The attached image(s) show the SPECIFIC vehicle model(s) this campaign targets.
Use them as VISUAL CONTEXT — match the body style, proportions, colour, and
era of the vehicle when a vehicle appears in your generated scene.
Do NOT copy the reference photo's composition or background. The reference
is purely for vehicle identification — your output is a NEW advertising
scene per the SUBJECT description above. If the reference shows a partial
view (e.g. front grille only), still infer the rest of the vehicle from it."""

    return f"""PHOTOREALISTIC automotive advertisement photograph. Documentary or studio photography, NEVER illustrated or rendered or 3D-CGI looking.{refs_block}

SUBJECT: {scene}

STRICT VISUAL RULES — these are non-negotiable:
1. Photorealistic photography only. No illustration, no painting, no 3D render aesthetic.
2. ZERO text or numbers in the image. No words, no letters, no digits anywhere.
3. ZERO logos, ZERO brand marks, ZERO printed labels on products or boxes.
4. ZERO cartoon icons, NO drawn symbols, NO illustrated badges, NO sketched graphics.
5. Any cardboard boxes shown must be PLAIN SOLID-COLOR cardboard — completely blank surfaces, no print, no graphics, no text whatsoever. Treat boxes like blank product packaging.
6. Products (brake discs, filters, pads, etc.) must look like real-world OEM-quality automotive parts — clean modern industrial design, not stylised or fantastical.
7. Lighting must be physically plausible — real shadows, real reflections, no impossible glow.
8. Composition: keep the LOWER THIRD visually quiet (uncluttered, neutral) — text and logo go there in post-production.

CONTEXTUAL ANCHORS:
- Market: South African automotive market, {branch_city} workshop / shop environment.
- Vehicle fitment context: {brands_str}-segment vehicles (Korean compact and mid-size cars).
- Brand palette accents only (very subtle): navy blue, emerald green.

ANTI-AI-TELL: avoid these common AI artefacts — distorted hands or fingers, garbled fake text, oversized fake logos on boxes, cartoonish painted-on icons, melting parts, impossible reflections, plastic-looking studio backdrop, overly saturated colours, generic stock-photo flatness."""


def _call_openai_image(prompt: str, model: str, quality: str) -> bytes:
    from openai import OpenAI  # lazy import
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    if model == "dall-e-3":
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality=quality if quality in ("standard", "hd") else "standard",
            n=1,
            response_format="b64_json",
        )
        return base64.b64decode(resp.data[0].b64_json)

    # default: gpt-image-1
    resp = client.images.generate(
        model=model,
        prompt=prompt,
        size="1024x1024",
        quality=quality if quality in ("low", "medium", "high") else "medium",
        n=1,
    )
    return base64.b64decode(resp.data[0].b64_json)


def _call_openai_image_with_refs(
    prompt: str,
    model: str,
    quality: str,
    reference_paths: list[Path],
) -> bytes:
    """Use gpt-image-1's edit endpoint with multiple reference images.

    The model treats these as visual context (vehicle look, brand feel) and
    generates a new ad-style image guided by the prompt.

    Note: only gpt-image-1 supports multi-image input. dall-e-3 falls back to
    text-only generation.
    """
    if model != "gpt-image-1":
        log.info("Refs supplied but model=%s doesn't support multi-image edit — falling back to text-only.", model)
        return _call_openai_image(prompt, model, quality)

    from openai import OpenAI  # lazy import
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    files = []
    try:
        for p in reference_paths:
            files.append(open(p, "rb"))
        resp = client.images.edit(
            model="gpt-image-1",
            image=files,
            prompt=prompt,
            size="1024x1024",
            quality=quality if quality in ("low", "medium", "high") else "high",
            n=1,
        )
    finally:
        for f in files:
            try:
                f.close()
            except Exception:
                pass

    return base64.b64decode(resp.data[0].b64_json)


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    )
    for fp in candidates:
        try:
            return ImageFont.truetype(fp, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        test = (current + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = w
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def overlay_branding(
    base_png: bytes,
    headline: str,
    cta: str,
    branch_name: str,
    branch_landline: Optional[str] = None,
) -> bytes:
    """Pillow overlay → final 1080x1080 PNG bytes."""
    img = Image.open(io.BytesIO(base_png)).convert("RGB")
    img = img.resize((1080, 1080), Image.LANCZOS)
    img = img.convert("RGBA")

    # Bottom gradient for text readability
    gradient = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(gradient)
    grad_top = 600
    for y in range(grad_top, 1080):
        alpha = int(((y - grad_top) / (1080 - grad_top)) ** 1.4 * 220)
        gd.line([(0, y), (1080, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img, gradient)

    draw = ImageDraw.Draw(img)

    # Logo top-left — transparent bg + soft white halo, blends on any image
    logo_block = _prepare_logo(target_height=140)
    if logo_block is not None:
        try:
            img.alpha_composite(logo_block, (28, 28))
        except Exception as e:
            log.warning("Logo overlay failed: %s", e)

    # Headline (center, in bottom region)
    font_headline = _load_font(64, bold=True)
    max_w = 960
    lines = _wrap_text(headline.upper() if headline else "", font_headline, max_w, draw)
    line_h = 80
    total_h = line_h * len(lines)
    y = 870 - total_h  # leave space for CTA below
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font_headline)
        w = bbox[2] - bbox[0]
        x = (1080 - w) // 2
        # subtle shadow
        draw.text((x + 2, y + 2), line, font=font_headline, fill=(0, 0, 0, 220))
        draw.text((x, y), line, font=font_headline, fill=WHITE)
        y += line_h

    # CTA pill (WhatsApp green) — bottom center
    font_cta = _load_font(40, bold=True)
    cta_text = (cta or "Send WhatsApp").upper()
    bbox = draw.textbbox((0, 0), cta_text, font=font_cta)
    cw, ch = bbox[2] - bbox[0], bbox[3] - bbox[1]
    btn_w = cw + 80
    btn_h = ch + 36
    btn_x = (1080 - btn_w) // 2
    btn_y = 920
    draw.rounded_rectangle(
        [(btn_x, btn_y), (btn_x + btn_w, btn_y + btn_h)],
        radius=18,
        fill=WHATSAPP_GREEN,
    )
    draw.text(
        (btn_x + 40, btn_y + 14),
        cta_text,
        font=font_cta,
        fill=WHITE,
    )

    # Branch line (small, very bottom)
    font_small = _load_font(28, bold=False)
    branch_line = branch_name
    if branch_landline:
        branch_line = f"{branch_name}  ·  {branch_landline}"
    bbox = draw.textbbox((0, 0), branch_line, font=font_small)
    bw = bbox[2] - bbox[0]
    draw.text(
        ((1080 - bw) // 2, 1020),
        branch_line,
        font=font_small,
        fill=(255, 255, 255, 220),
    )

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def generate_poster_for_variant(creative_id: int) -> Optional[str]:
    """Generate poster for a single ad_creative. Returns relative path or None on failure."""
    if not is_enabled():
        log.info("OPENAI_API_KEY not set — poster generation disabled.")
        return None

    with db() as conn:
        c = conn.execute(
            "SELECT * FROM ad_creatives WHERE id = ?", (creative_id,)
        ).fetchone()
        if not c:
            return None
        camp = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (c["campaign_id"],)
        ).fetchone()
        if not camp:
            return None
        branch_codes = json.loads(camp["branch_codes"])
        primary_branch = branch_codes[0] if branch_codes else "boksburg"
        branch = conn.execute(
            "SELECT * FROM branches WHERE code = ?", (primary_branch,)
        ).fetchone()
        if not branch:
            return None

    brands = json.loads(camp["brands"])

    # Find brand-matched reference images (e.g. i20.jpg, sportage.png)
    refs = _find_reference_vehicle_images(brands)

    prompt = build_visual_prompt(
        c["angle"] or "local",
        brands,
        branch["city"],
        has_references=bool(refs),
    )

    model = os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_MODEL)
    quality = os.getenv("OPENAI_IMAGE_QUALITY", DEFAULT_QUALITY)
    if model == "dall-e-3":
        quality = "hd"

    log.info(
        "AssetGen: variant #%s (angle=%s, model=%s, quality=%s, refs=%d) [%s]",
        creative_id, c["angle"], model, quality, len(refs),
        ", ".join(p.name for p in refs) if refs else "no refs",
    )
    try:
        if refs:
            base = _call_openai_image_with_refs(prompt, model, quality, refs)
        else:
            base = _call_openai_image(prompt, model, quality)
    except Exception as e:
        log.error("OpenAI image API failed: %s", e)
        return None

    try:
        final = overlay_branding(
            base,
            headline=c["headline"] or "",
            cta=c["cta"] or "Send WhatsApp",
            branch_name=branch["name"],
            branch_landline=branch["landline"],
        )
    except Exception as e:
        log.error("Pillow overlay failed: %s", e)
        # Save raw base image as fallback so the spend isn't wasted
        final = base

    fname = f"variant_{creative_id}_{secrets.token_hex(4)}.png"
    fpath = GENERATED_DIR / fname
    fpath.write_bytes(final)
    rel = str(fpath.relative_to(ROOT))

    with db() as conn:
        conn.execute(
            "UPDATE ad_creatives SET asset_path = ? WHERE id = ?",
            (rel, creative_id),
        )

    log.info("AssetGen: variant #%s saved to %s", creative_id, rel)
    return rel
