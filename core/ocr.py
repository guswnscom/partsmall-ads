"""License disc OCR -> chassis (VIN) extraction via Claude Vision.

남아공 차량 license disc 사진에서 VIN (chassis number) 17자리 추출.
ANTHROPIC_API_KEY 가 .env 에 있어야 동작. 없으면 None 반환 (graceful skip).

비용: claude-haiku 모델 + 작은 이미지 = 호출당 약 $0.001~0.003 (R0.02~0.06).
하루 100건 OCR 해도 R6 미만 → MVP 예산에 무시 가능.
"""

from __future__ import annotations
import base64
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Standard VIN: 17 chars, uppercase A-Z + 0-9, excluding I/O/Q
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

DEFAULT_MODEL = "claude-haiku-4-5"

PROMPT = """You are an OCR specialist reading a South African vehicle license disc photo.
Extract ONLY the VIN / chassis number. The VIN is exactly 17 characters,
uppercase letters and digits (letters I, O, Q are never used).
On a SA license disc it is usually labeled "VIN" or "Chassis No".

Output rules — STRICT:
- If you can read the VIN clearly, output it as a single line: just the 17 characters.
- If the photo is unclear, blurry, cropped, or the VIN is not visible, output exactly: NONE
- No explanations, no extra words, no markdown.
"""


def _media_type_from_path(p: Path) -> str:
    ext = p.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(ext, "image/jpeg")


def extract_vin_from_image(image_path: str | Path) -> Optional[str]:
    """Return the 17-char VIN if confidently extracted, else None.

    Returns None when:
      - ANTHROPIC_API_KEY 없음
      - 모델이 NONE 응답 또는 형식이 안 맞을 때
      - 네트워크/API 에러
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.info("ANTHROPIC_API_KEY not set — skipping license disc OCR.")
        return None

    p = Path(image_path)
    if not p.exists():
        return None

    try:
        from anthropic import Anthropic  # lazy import
    except Exception as e:
        log.warning("anthropic SDK not installed: %s", e)
        return None

    img_b64 = base64.standard_b64encode(p.read_bytes()).decode("ascii")
    media_type = _media_type_from_path(p)
    model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)

    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=64,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )
    except Exception as e:
        log.warning("Vision API call failed: %s", e)
        return None

    # Extract text
    text_parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    raw = "".join(text_parts).strip().upper()

    if not raw or raw == "NONE":
        return None

    # Be defensive — model might add stray chars; try regex extraction.
    match = VIN_RE.search(raw)
    if match:
        return match.group(0)

    # Fall back: if response is exactly 17 valid chars, accept it
    if len(raw) == 17 and VIN_RE.fullmatch(raw):
        return raw

    return None
