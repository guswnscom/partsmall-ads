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

from PIL import Image, ImageDraw, ImageFont

from .db import db

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "assets"
GENERATED_DIR = ROOT / "generated"
GENERATED_DIR.mkdir(exist_ok=True)

LOGO_PATH = ASSETS_DIR / "logo.png"

DEFAULT_MODEL = "gpt-image-1"
DEFAULT_QUALITY = "medium"  # gpt-image-1: low/medium/high
FALLBACK_DALLE_QUALITY = "standard"  # dall-e-3: standard/hd

# Brand colors
NAVY = (30, 58, 138)        # #1e3a8a
EMERALD = (22, 163, 74)     # #16a34a
WHATSAPP_GREEN = (37, 211, 102)  # #25D366
WHITE = (255, 255, 255)

# Per-angle visual scene descriptors. Each must produce ad-friendly imagery
# without text, with bottom-third left clean for our overlay.
ANGLE_SCENES: Dict[str, str] = {
    "stock": (
        "neatly stacked Korean automotive part boxes on bright clean warehouse shelves, "
        "rich product variety visible, sense of abundant inventory"
    ),
    "trust": (
        "premium brake disc and brake pads set arranged on a clean studio backdrop, "
        "soft directional lighting, flagship product photography feel, premium quality cues"
    ),
    "urgency": (
        "boxed automotive part on a clean parts counter with a hand reaching for it, "
        "shallow depth of field, sense of immediacy and same-day availability"
    ),
    "service": (
        "skilled mechanic's hands installing a fresh OEM-quality automotive part in a bright "
        "modern workshop, blue overalls, clean workbench, professional service feel"
    ),
    "value": (
        "automotive workshop with neatly organised Korean parts on display next to professional "
        "tools, bright natural lighting, trust and fair-pricing atmosphere"
    ),
    "safety": (
        "Korean sedan or hatchback (Hyundai or Kia silhouette, no logos) on a sunlit South African "
        "road, mountains in distance, sense of confidence and reliability"
    ),
    "local": (
        "friendly automotive parts shop counter scene, warm lighting, welcoming atmosphere, "
        "sense of local community service"
    ),
}


def is_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def build_visual_prompt(
    angle: str,
    brands: list[str],
    branch_city: str,
) -> str:
    angle_key = (angle or "local").lower()
    scene = ANGLE_SCENES.get(angle_key, ANGLE_SCENES["local"])
    brands_str = ", ".join(brands[:3]) if brands else "Korean automotive"
    return (
        "Professional automotive parts advertisement photograph. "
        f"Scene: {scene}. "
        f"Featured brand context: {brands_str} (Korean OEM-equivalent parts), "
        f"South African market ({branch_city}). "
        "Style: clean, premium, magazine-quality automotive product photography, "
        "navy blue and emerald green brand palette (subtle accents only), "
        "neutral or white background where possible. "
        "Composition: keep the LOWER THIRD of the frame visually quiet — "
        "we add headline text and brand logo in post-production. "
        "ABSOLUTE RULES: do NOT include any text, words, letters, numbers, signage, "
        "watermarks, fake logos, or labels of any kind in the image. "
        "No brand names visible. No fake product packaging text."
    )


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

    # Logo top-left
    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            target_h = 110
            ratio = target_h / logo.height
            logo = logo.resize((int(logo.width * ratio), target_h), Image.LANCZOS)
            # white pill background for logo readability over any image
            pad = 16
            pill = Image.new(
                "RGBA",
                (logo.width + 2 * pad, logo.height + 2 * pad),
                (255, 255, 255, 235),
            )
            pmask = Image.new("L", pill.size, 0)
            pdraw = ImageDraw.Draw(pmask)
            pdraw.rounded_rectangle([(0, 0), pill.size], radius=20, fill=255)
            img.paste(pill, (40, 40), pmask)
            img.paste(logo, (40 + pad, 40 + pad), logo)
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
    prompt = build_visual_prompt(c["angle"] or "local", brands, branch["city"])

    model = os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_MODEL)
    quality = os.getenv("OPENAI_IMAGE_QUALITY", DEFAULT_QUALITY)
    if model == "dall-e-3":
        quality = "standard"

    log.info(
        "AssetGen: variant #%s (angle=%s, model=%s, quality=%s)",
        creative_id, c["angle"], model, quality,
    )
    try:
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
