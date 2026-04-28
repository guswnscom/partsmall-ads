"""Round-robin staff assignment + WhatsApp deep link builder.

전략:
- 한 지점 안에서 active=1 인 직원 중 last_assigned_at 가장 오래된 사람 선택
- 영업시간 외에는 "after-hours" 응답 모드 (다음 영업일 안내)
"""

from __future__ import annotations
import os
import sqlite3
from datetime import datetime, time, timedelta
from typing import Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .db import db


SAST = ZoneInfo("Africa/Johannesburg")

# Business hours: Mon-Fri 08:00-17:00, Sat 08:00-13:00, Sun closed
BUSINESS_HOURS = {
    0: (time(8, 0), time(17, 0)),  # Mon
    1: (time(8, 0), time(17, 0)),
    2: (time(8, 0), time(17, 0)),
    3: (time(8, 0), time(17, 0)),
    4: (time(8, 0), time(17, 0)),  # Fri
    5: (time(8, 0), time(13, 0)),  # Sat
    6: None,                        # Sun
}


def now_sast() -> datetime:
    return datetime.now(SAST)


def is_open(dt: Optional[datetime] = None) -> bool:
    dt = dt or now_sast()
    hours = BUSINESS_HOURS.get(dt.weekday())
    if hours is None:
        return False
    start, end = hours
    return start <= dt.time() <= end


def next_open_message() -> str:
    """English message to prefill if customer clicks outside business hours."""
    dt = now_sast()
    # find next open weekday slot
    for i in range(1, 8):
        candidate = dt + timedelta(days=i)
        hours = BUSINESS_HOURS.get(candidate.weekday())
        if hours:
            day_name = candidate.strftime("%A")
            return f"Our team will reply when we open on {day_name} at 08:00 SAST."
    return "Our team will reply during business hours."


def pick_staff(branch_code: str) -> Optional[sqlite3.Row]:
    """Round-robin: pick the active staff member at branch with oldest last_assigned_at."""
    with db() as conn:
        row = conn.execute(
            """SELECT s.* FROM staff s
               JOIN branches b ON b.id = s.branch_id
               WHERE b.code = ? AND s.active = 1
               ORDER BY COALESCE(s.last_assigned_at, '1970-01-01') ASC, s.id ASC
               LIMIT 1""",
            (branch_code,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE staff SET last_assigned_at = ? WHERE id = ?",
                (now_sast().isoformat(), row["id"]),
            )
        return row


def build_whatsapp_url(
    e164: str,
    branch_name: str,
    brand: Optional[str] = None,
    part: Optional[str] = None,
    vin: Optional[str] = None,
    extracted_vin: Optional[str] = None,
    photo_urls: Optional[list] = None,
) -> str:
    """Build wa.me deep link with English prefilled message.

    e164: '+27720229164' -> strip '+' for wa.me
    photo_urls: list of (label, url) tuples appended at the end of the message.
    extracted_vin: OCR'd VIN from license disc (separate from customer-typed `vin`).
    """
    number = e164.lstrip("+").replace(" ", "")

    parts = ["Hi PARTS-MALL"]
    if brand:
        parts.append(f"I'm looking for {brand} parts")
    if part:
        parts.append(f"specifically: {part}")
    if vin:
        parts.append(f"VIN (typed): {vin}")
    if extracted_vin and extracted_vin != vin:
        parts.append(f"VIN (auto-read from license disc): {extracted_vin}")
    elif photo_urls and any(lbl == "License disc" for lbl, _ in photo_urls) and not extracted_vin:
        # license disc uploaded but OCR failed/uncertain — staff must read it manually
        parts.append("License disc attached — VIN could not be auto-read, please check photo")
    parts.append(f"(saw your ad — {branch_name} branch)")

    if not is_open():
        parts.append(next_open_message())

    text = ". ".join(parts) + "."

    if photo_urls:
        text += "\n\nAttached:"
        for label, url in photo_urls:
            text += f"\n- {label}: {url}"

    return f"https://wa.me/{number}?text={quote(text)}"
