"""Daily cron entry-point for Director Agent.

Runs at 06:00 SAST every day. For each ACTIVE campaign with stale or missing
ad copy, generate fresh 5 variants. Manager reviews in admin UI.

Strategy:
  - Eligible: status IN ('approved','live') AND
              (no ad_creatives OR newest creative created_at > 7 days ago)
  - Skip: campaign with > 5 PENDING (unreviewed) creatives — manager needs to clear queue
  - Logs: stdout via journalctl
"""

from __future__ import annotations
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# allow running as `python -m core.cron_director` from /opt/partsmall
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env so ANTHROPIC_API_KEY etc are present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from core.db import db, init_db
from core.director import generate_and_save

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("director-cron")

STALE_DAYS = 7
PENDING_THRESHOLD = 5


def run() -> int:
    init_db()
    cutoff_iso = (datetime.utcnow() - timedelta(days=STALE_DAYS)).isoformat()

    with db() as conn:
        campaigns = conn.execute(
            "SELECT * FROM campaigns WHERE status IN ('approved','live') ORDER BY id"
        ).fetchall()

    log.info("Found %d active campaigns to evaluate.", len(campaigns))
    generated = 0

    for c in campaigns:
        with db() as conn:
            newest = conn.execute(
                """SELECT MAX(created_at) AS m FROM ad_creatives
                   WHERE campaign_id = ?""",
                (c["id"],),
            ).fetchone()
            pending_count = conn.execute(
                """SELECT COUNT(*) AS n FROM ad_creatives
                   WHERE campaign_id = ? AND approved = 0 AND rejected_at IS NULL""",
                (c["id"],),
            ).fetchone()["n"]

        if pending_count >= PENDING_THRESHOLD:
            log.info(
                "Campaign #%s has %d pending creatives — skipping (manager queue full).",
                c["id"], pending_count,
            )
            continue

        if newest["m"] and newest["m"] >= cutoff_iso:
            log.info(
                "Campaign #%s has fresh creatives (newest %s) — skipping.",
                c["id"], newest["m"],
            )
            continue

        try:
            n = generate_and_save(c["id"])
            log.info("Campaign #%s — generated %d variants.", c["id"], n)
            generated += n
        except Exception as e:
            log.error("Campaign #%s — generation failed: %s", c["id"], e)

    log.info("Done. Generated %d total variants.", generated)
    return generated


if __name__ == "__main__":
    sys.exit(0 if run() >= 0 else 1)
