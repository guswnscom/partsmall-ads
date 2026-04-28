"""Seed initial branches + staff for Boksburg and Edenvale.

직원 번호는 +27 형식으로 정규화. 지점 좌표는 Google Maps 기준 대략값.
"""

from __future__ import annotations
from .db import db, init_db


# (code, name, city, address, landline, lat, lng, radius_km)
BRANCHES = [
    (
        "boksburg",
        "PARTS-MALL Boksburg",
        "Boksburg",
        "Shop No.11, Venter Centre, Corner N Rand Rd & Rietfontein Rd, Boksburg, 1620",
        "011 823 2610 / 1655",
        -26.2125, 28.2624, 10,
    ),
    (
        "edenvale",
        "PARTS-MALL Edenvale",
        "Edenvale",
        "123 Van Riebeeck Ave, Edenvale, Johannesburg, 1609",
        "011 462 0332",
        -26.1396, 28.1546, 8,
    ),
]

# (branch_code, name, e164, number_type)
STAFF = [
    ("boksburg", "Neil Fourie", "+27720229164", "personal"),
    ("boksburg", "Nicholas",    "+27660674397", "company"),
    ("boksburg", "Xolani",      "+27659627180", "personal"),
    ("boksburg", "Chalie",      "+27660827417", "company"),
    ("edenvale", "Salim Hasane", "+27660030842", "company"),
    ("edenvale", "Ayanda",       "+27616217199", "personal"),
]


def seed() -> None:
    init_db()
    with db() as conn:
        for code, name, city, address, landline, lat, lng, radius in BRANCHES:
            conn.execute(
                """INSERT INTO branches
                   (code, name, city, address, landline, lat, lng, radius_km)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, city=excluded.city,
                     address=excluded.address, landline=excluded.landline,
                     lat=excluded.lat, lng=excluded.lng, radius_km=excluded.radius_km""",
                (code, name, city, address, landline, lat, lng, radius),
            )

        for branch_code, name, e164, ntype in STAFF:
            row = conn.execute(
                "SELECT id FROM branches WHERE code = ?", (branch_code,)
            ).fetchone()
            if not row:
                continue
            branch_id = row["id"]
            existing = conn.execute(
                "SELECT id FROM staff WHERE branch_id = ? AND name = ?",
                (branch_id, name),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE staff SET whatsapp_e164=?, number_type=?, active=1
                       WHERE id=?""",
                    (e164, ntype, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO staff (branch_id, name, whatsapp_e164, number_type)
                       VALUES (?, ?, ?, ?)""",
                    (branch_id, name, e164, ntype),
                )
    print("Seeded branches + staff.")


if __name__ == "__main__":
    seed()
