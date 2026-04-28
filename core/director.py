"""Director Agent — generates English ad copy variants via Claude.

Inputs:
  - Campaign metadata (brands, branch, manager note)
  - reference/ folder (brand voice .md files + past ad images)
  - Recent click_log analytics (last 7 days top performers)
  - Recent approved ad_creatives (last 30 days, avoid repetition)

Output:
  - 5 distinct ad variants, each with audience tag, headline, primary text, CTA, hashtags
  - Saved to ad_creatives table with approved=0 (manager must approve)

Cost rough estimate: ~$0.01 per generation call (Sonnet, ~2k input + 1k output tokens).
"""

from __future__ import annotations
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import db

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "reference"

DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_REF_IMAGES = 3
MAX_REF_TEXT_BYTES = 60_000  # cap so prompt doesn't explode

# Headline 40 chars / primary 125 chars are Meta-friendly soft limits.
SYSTEM_PROMPT = """You are the Director of Advertising for PARTS-MALL South Africa,
a specialised Korean auto parts retailer with branches in Boksburg and Edenvale.
PMC (Parts-Mall Corporation, our parent brand) parts are exported to 70+ countries.

Your job: generate Meta (Facebook + Instagram) ad copy in clear, natural South African English.

Priority brands (these always come first when relevant): Hyundai, Kia, Chevrolet, Suzuki, Ssangyong.

You serve TWO audiences and must produce variants targeting each:

  WORKSHOPS (B2B): independent mechanics, panel beaters, fleet managers.
    They care about — stock availability, accounts/credit, fast pickup, OEM-equivalent quality at right price.
    Tone: professional, peer-to-peer, "we keep your bays moving".

  DRIVERS (B2C): individual car owners across SA.
    They care about — trust ("genuine parts"), fair price, easy WhatsApp contact,
    convenient location, friendly service, safety, no broken English.
    Tone: confident specialist friend, not pushy salesman.

Brand voice rules:
- South African English (use "specialised", "tyre", "kilometre" if used)
- Always anchor to a branch city (Boksburg or Edenvale) when relevant
- Always offer WhatsApp as the contact channel
- Headline: max 40 characters
- Primary text: max 125 characters (Meta first-line preview length)
- CTA must be one of: "Send WhatsApp", "Get Quote", "Find Parts", "Book Service"
- At most ONE emoji per variant — only if it adds something
- Avoid: "Cheap", "Buy now!", aggressive sales language, generic "auto parts"
- Prefer: "in stock today", "specialised Korean parts", "OEM-standard quality",
  "WhatsApp us now", "safety-engineered", "globally trusted (70+ countries)"

CRITICAL LEGAL & TRADEMARK RULES (NON-NEGOTIABLE):
- NEVER write "Genuine Hyundai parts", "Genuine Kia parts", or "Genuine [brand] parts".
  The word "Genuine" on its own implies OEM-authentic parts — we sell OEM-equivalent /
  OEM-standard quality, NOT necessarily original-OEM. Using "Genuine" alone is a
  trademark and false-advertising risk in South Africa.
- ALWAYS use one of these legally-safe phrasings instead:
    "Genuine quality" (qualifying quality, not authenticity)
    "OEM-standard quality"
    "OEM-equivalent quality"
    "PMC quality" (our parent brand)
    "Specialised Korean parts"
- Never claim our parts are "made by Hyundai/Kia/Chevrolet/Suzuki/Ssangyong".
  We are a SPECIALISED DISTRIBUTOR of parts that FIT those brands.
- Brand names (Hyundai, Kia, etc.) may appear ONLY as fitment context
  (e.g. "parts for your Hyundai i20", "Kia Sportage parts in stock"),
  never as the source of our parts.
- Avoid words that imply official endorsement: "Authorised", "Certified by",
  "Approved by [brand]" (unless legally true and verified).

Service / maintenance angle — IMPORTANT pattern to include:
Some variants must speak to drivers who are due for service. Example phrasings:
  - "Missed your service? OEM-standard parts ready today."
  - "Time to service your Hyundai/Kia? Specialised parts in stock."
  - "Don't delay your safety check — OEM-grade parts at PARTS-MALL Boksburg."
Lean on safety, OEM-quality assurance, and ease (one WhatsApp message).

Output rules:
- Return ONLY a valid JSON array of 5 objects, no preamble, no code fences, no commentary.
- Each variant differs in audience and angle — never duplicate phrasing.
"""


def _load_text_references() -> str:
    if not REF_DIR.exists():
        return ""
    chunks: List[str] = []
    total_bytes = 0
    for p in sorted(REF_DIR.glob("**/*")):
        if not p.is_file() or p.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        chunk = f"=== {p.relative_to(REF_DIR)} ===\n{text}"
        if total_bytes + len(chunk) > MAX_REF_TEXT_BYTES:
            chunks.append(chunk[: MAX_REF_TEXT_BYTES - total_bytes])
            break
        chunks.append(chunk)
        total_bytes += len(chunk)
    return "\n\n".join(chunks)


def _load_image_references() -> List[Dict[str, Any]]:
    if not REF_DIR.exists():
        return []
    img_paths = sorted(
        p
        for p in REF_DIR.glob("**/*")
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    )[:MAX_REF_IMAGES]

    blocks: List[Dict[str, Any]] = []
    for p in img_paths:
        try:
            data = base64.standard_b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            continue
        suffix = p.suffix.lower().lstrip(".")
        media = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(suffix, f"image/{suffix}")
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": data},
            }
        )
    return blocks


def _recent_top_performers(days: int = 7) -> str:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT brand, COUNT(*) AS clicks
               FROM click_logs WHERE clicked_at >= ?
               GROUP BY brand ORDER BY clicks DESC LIMIT 6""",
            (since,),
        ).fetchall()
    if not rows:
        return "(no click data yet — generate balanced variants across priority brands)"
    return "\n".join(f"- {r['brand']}: {r['clicks']} clicks" for r in rows)


def _past_copies(days: int = 30, limit: int = 25) -> str:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT headline, primary_text FROM ad_creatives
               WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
    if not rows:
        return "(none yet)"
    return "\n".join(
        f"- {r['headline']!r} / {r['primary_text']!r}"
        for r in rows
        if r["headline"]
    )


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove leading ```json or ```
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        # remove trailing ```
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def generate_ad_copies(campaign_id: int, n_variants: int = 5) -> List[Dict[str, Any]]:
    """Call Claude to generate N ad variants for the given campaign.

    Raises on API failure; caller decides how to handle.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

    from anthropic import Anthropic  # lazy import

    with db() as conn:
        camp = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if not camp:
            raise ValueError(f"Campaign #{campaign_id} not found")
        branch_codes = json.loads(camp["branch_codes"])
        placeholders = ",".join("?" * len(branch_codes))
        branches = conn.execute(
            f"SELECT * FROM branches WHERE code IN ({placeholders})",
            tuple(branch_codes),
        ).fetchall()

    brands = json.loads(camp["brands"])
    branches_str = "\n".join(
        f"- {b['name']} ({b['city']}) — {b['address'] or 'address TBD'}"
        f" | landline {b['landline'] or '-'}"
        for b in branches
    )

    user_msg_parts = []

    text_refs = _load_text_references()
    if text_refs:
        user_msg_parts.append(
            "Brand voice references (from reference/ folder — match this tone):\n"
            f"{text_refs}\n"
        )

    user_msg_parts.append(
        f"""Campaign brief:
- Title: {camp['title']}
- Brands featured: {', '.join(brands)}
- Branches:
{branches_str}
- Manager note: {camp['notes'] or '(none)'}

Recent click performance (last 7 days, by brand):
{_recent_top_performers()}

Recent ad copy already generated (last 30 days — DO NOT repeat phrasing or angles):
{_past_copies()}

Generate exactly {n_variants} distinct Meta ad variants. Cover ALL of these audiences/angles:
1. WORKSHOP B2B — stock/account focus ("we keep your bays moving")
2. DRIVER B2C — trust / **Genuine quality** / OEM-standard / "globally trusted in 70+ countries"
3. DRIVER B2C — SERVICE reminder ("Missed your service? OEM-standard parts ready today, WhatsApp us")
4. WORKSHOP B2B — fair pricing / fast pickup / no dealer markup
5. UNIVERSAL — local + WhatsApp + safety-engineered Korean parts

⚠️ Trademark check before each variant: "Did I write 'Genuine [brand]' or 'Genuine
parts' in a way that implies OEM-authentic?" If yes, REWRITE using "Genuine quality",
"OEM-standard quality", or "Specialised Korean parts" instead.

For variant #3 specifically, lean on the service/maintenance angle — a customer overdue
for service should feel "I should sort this out today, with parts I can trust." Use
phrases close to: "Missed your service?", "Time to service your i20?", "Don't delay
safety", "OEM-standard quality from a globally trusted name (PMC)".

Each variant must be a JSON object with these exact fields:
- audience: "workshop" | "driver" | "both"
- angle: short label (e.g. "stock", "trust", "service", "value", "safety", "local")
- headline: string, max 40 characters
- primary_text: string, max 125 characters
- cta: "Send WhatsApp" | "Get Quote" | "Find Parts" | "Book Service"
- hashtags: array of 3 to 5 strings (no # prefix)

Return ONLY a JSON array of {n_variants} objects. No code fences. No commentary."""
    )

    user_text = "\n\n---\n\n".join(user_msg_parts)

    content: List[Dict[str, Any]] = _load_image_references()
    if content:
        log.info("Director: included %d reference image(s)", len(content))
    content.append({"type": "text", "text": user_text})

    model = os.getenv("ANTHROPIC_COPY_MODEL", DEFAULT_MODEL)
    client = Anthropic(api_key=api_key)
    log.info("Director: calling %s for campaign #%s", model, campaign_id)

    msg = client.messages.create(
        model=model,
        max_tokens=2500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    text = _strip_code_fence(text)

    try:
        variants = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Director: model returned invalid JSON: %s\n---\n%s", e, text[:1000])
        raise RuntimeError(f"Director output was not valid JSON: {e}")

    if not isinstance(variants, list):
        raise RuntimeError("Director output was not a JSON array")

    # Defensive normalization
    cleaned: List[Dict[str, Any]] = []
    for v in variants[:n_variants]:
        if not isinstance(v, dict):
            continue
        cleaned.append(
            {
                "audience": str(v.get("audience", "both"))[:32],
                "angle": str(v.get("angle", "general"))[:32],
                "headline": str(v.get("headline", ""))[:80],
                "primary_text": str(v.get("primary_text", ""))[:300],
                "cta": str(v.get("cta", "Send WhatsApp"))[:32],
                "hashtags": [str(h)[:48] for h in v.get("hashtags", []) if h][:5],
            }
        )
    return cleaned


def save_variants(
    campaign_id: int,
    variants: List[Dict[str, Any]],
    landing_base: Optional[str] = None,
) -> int:
    """Insert generated variants into ad_creatives. All approved=0 by default."""
    landing_base = landing_base or os.getenv(
        "LANDING_BASE_URL", "https://psms-pmaad.co.za"
    )
    saved = 0
    with db() as conn:
        camp = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if not camp:
            raise ValueError(f"Campaign #{campaign_id} not found")
        branch_codes = json.loads(camp["branch_codes"])
        primary_branch = branch_codes[0] if branch_codes else "boksburg"
        for v in variants:
            angle = re.sub(r"[^a-z0-9]+", "_", v["angle"].lower())[:24] or "default"
            landing_url = (
                f"{landing_base}/{primary_branch}"
                f"?cid={campaign_id}&utm_source=meta&utm_campaign=cid{campaign_id}_{angle}"
            )
            conn.execute(
                """INSERT INTO ad_creatives
                   (campaign_id, platform, audience, angle,
                    headline, primary_text, cta, hashtags,
                    landing_url, generated_by, approved, created_at)
                   VALUES (?, 'meta', ?, ?, ?, ?, ?, ?, ?, 'director_agent', 0, ?)""",
                (
                    campaign_id,
                    v.get("audience", "both"),
                    v.get("angle", "general"),
                    v.get("headline", ""),
                    v.get("primary_text", ""),
                    v.get("cta", "Send WhatsApp"),
                    json.dumps(v.get("hashtags", []), ensure_ascii=False),
                    landing_url,
                    datetime.utcnow().isoformat(),
                ),
            )
            saved += 1
    return saved


def generate_and_save(campaign_id: int, n_variants: int = 5) -> int:
    """Convenience: generate + save in one call. Returns count saved."""
    variants = generate_ad_copies(campaign_id, n_variants=n_variants)
    return save_variants(campaign_id, variants)
