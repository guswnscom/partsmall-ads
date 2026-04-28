"""SQLite schema + connection helper.

Tables:
- branches    : 지점 (Boksburg, Edenvale, ...)
- staff       : 직원 (지점별 WhatsApp 번호)
- campaigns   : 매니저가 업로드한 광고 캠페인 (포스터 + 메타데이터)
- ad_creatives: 캠페인에서 파생된 플랫폼별 카피/에셋 (Director Agent 산출물)
- click_logs  : 랜딩페이지 → WhatsApp 리다이렉트 클릭 추적
- assignments : 라운드로빈 할당 카운터 (직원별 최근 할당 시각)
"""

from __future__ import annotations
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "./data/partsmall.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE NOT NULL,        -- 'boksburg', 'edenvale'
    name        TEXT NOT NULL,               -- 'Boksburg', 'Edenvale'
    city        TEXT NOT NULL,
    address     TEXT,                        -- full street address
    landline    TEXT,                        -- '011 823 2610 / 1655'
    lat         REAL,
    lng         REAL,
    radius_km   INTEGER DEFAULT 10,
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS staff (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id       INTEGER NOT NULL REFERENCES branches(id),
    name            TEXT NOT NULL,
    whatsapp_e164   TEXT NOT NULL,           -- '+27720229164' style
    number_type     TEXT DEFAULT 'unknown',  -- 'personal' | 'business' | 'company' | 'unknown'
    active          INTEGER DEFAULT 1,
    last_assigned_at TEXT                    -- ISO timestamp, used for round-robin
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    poster_path     TEXT NOT NULL,           -- uploaded original
    brands          TEXT NOT NULL,           -- JSON array: ["Hyundai","Kia"]
    branch_codes    TEXT NOT NULL,           -- JSON array: ["boksburg","edenvale"]
    draft_copy      TEXT,                    -- manager-provided copy seed (optional)
    status          TEXT DEFAULT 'draft',    -- draft | pending_approval | approved | live | archived
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS ad_creatives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    platform        TEXT NOT NULL,           -- 'meta' | 'google' | 'tiktok'
    audience        TEXT,                    -- 'workshop' | 'driver' | 'both'
    angle           TEXT,                    -- 'stock' | 'trust' | 'urgency' | 'value' | 'local'
    headline        TEXT,
    primary_text    TEXT,
    cta             TEXT,                    -- e.g. 'Send WhatsApp'
    hashtags        TEXT,                    -- JSON array
    asset_path      TEXT,                    -- watermarked poster
    landing_url     TEXT,                    -- per-region landing
    generated_by    TEXT,                    -- 'director_agent' | 'manual'
    approved        INTEGER DEFAULT 0,
    rejected_at     TEXT,
    approved_at     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS click_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code     TEXT NOT NULL,
    staff_id        INTEGER REFERENCES staff(id),
    campaign_id     INTEGER REFERENCES campaigns(id),
    brand           TEXT,
    part_query      TEXT,
    vin             TEXT,
    license_disc_path TEXT,
    part_photo_path TEXT,
    extracted_vin   TEXT,
    user_agent      TEXT,
    ip              TEXT,
    referrer        TEXT,
    utm_source      TEXT,
    utm_campaign    TEXT,
    clicked_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clicks_branch_time ON click_logs(branch_code, clicked_at);
CREATE INDEX IF NOT EXISTS idx_staff_branch_active ON staff(branch_id, active);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # idempotent migrations: add columns if missing on existing DBs
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(click_logs)")]
        for col in ("vin", "license_disc_path", "part_photo_path", "extracted_vin"):
            if col not in cols:
                conn.execute(f"ALTER TABLE click_logs ADD COLUMN {col} TEXT")
        # branches table: address + landline (added 2026-04)
        bcols = [r["name"] for r in conn.execute("PRAGMA table_info(branches)")]
        for col in ("address", "landline"):
            if col not in bcols:
                conn.execute(f"ALTER TABLE branches ADD COLUMN {col} TEXT")
        # ad_creatives: director-agent metadata columns (added 2026-04)
        accols = [r["name"] for r in conn.execute("PRAGMA table_info(ad_creatives)")]
        for col in ("audience", "angle", "hashtags", "rejected_at", "approved_at"):
            if col not in accols:
                conn.execute(f"ALTER TABLE ad_creatives ADD COLUMN {col} TEXT")


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH.resolve()}")
