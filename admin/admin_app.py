"""Streamlit admin UI — manager uploads posters and reviews campaigns/clicks.

탭 구성:
  1. Upload Campaign : 포스터 업로드 + 차종/지점/카피 초안 입력 → DB 저장 (status=pending_approval)
  2. Campaigns       : 목록 + 승인/보류/아카이브
  3. Staff           : 직원 번호 관리 (활성/비활성, 번호 수정)
  4. Analytics       : 지점별/직원별 클릭 통계

비밀번호: .env 의 ADMIN_PASSWORD (초기값 'change-me-monday' — 첫 로그인 후 바꿔라)
"""

from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.db import db, init_db  # noqa: E402
from core.seed import seed       # noqa: E402
from core.director import generate_and_save  # noqa: E402
from core.asset_generator import generate_poster_for_variant, is_enabled as poster_enabled  # noqa: E402


# Cost (USD) per generated image by model + quality.
_COST_USD_BY_KEY = {
    ("gpt-image-1", "low"):    0.011,
    ("gpt-image-1", "medium"): 0.042,
    ("gpt-image-1", "high"):   0.167,
    ("dall-e-3", "standard"):  0.040,
    ("dall-e-3", "hd"):        0.080,
}


def _poster_cost_label() -> str:
    """Return e.g. '~R3.20 ($0.17)' based on env-configured model+quality.
    USD->ZAR fallback to R19 if OPENAI_USD_TO_ZAR not set."""
    model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    quality = os.getenv("OPENAI_IMAGE_QUALITY", "high").lower()
    usd = _COST_USD_BY_KEY.get((model, quality), 0.167)
    try:
        rate = float(os.getenv("OPENAI_USD_TO_ZAR", "19"))
    except ValueError:
        rate = 19.0
    zar = usd * rate
    return f"~R{zar:.2f} (${usd:.2f})"

UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(exist_ok=True)

PRIORITY_BRANDS = ["Hyundai", "Kia", "Chevrolet", "Suzuki", "Ssangyong"]
OTHER_BRANDS = ["Toyota", "Ford", "Nissan", "Volkswagen", "Other"]
ALL_BRANDS = PRIORITY_BRANDS + OTHER_BRANDS


st.set_page_config(page_title="PARTS-MALL Admin", page_icon="🛠️", layout="wide")


# --- auth ---
def check_password() -> bool:
    expected = os.getenv("ADMIN_PASSWORD", "change-me-monday")
    if st.session_state.get("auth_ok"):
        return True
    with st.form("login"):
        pw = st.text_input("Admin password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok and pw == expected:
        st.session_state["auth_ok"] = True
        st.rerun()
    elif ok:
        st.error("Wrong password.")
    return False


# --- pages ---
def page_upload():
    st.header("📤 Upload Campaign")
    st.caption("Upload a poster, tag brands and branches. The Director Agent will draft ad copy in English for review.")

    with st.form("upload_form", clear_on_submit=True):
        title = st.text_input("Campaign title (internal)", placeholder="e.g. May Hyundai Brake Promo")
        poster = st.file_uploader("Poster image", type=["png", "jpg", "jpeg", "webp"])
        brands = st.multiselect("Car brands featured", ALL_BRANDS, default=["Hyundai", "Kia"])

        with db() as conn:
            branch_rows = conn.execute(
                "SELECT code, name FROM branches WHERE active=1 ORDER BY name"
            ).fetchall()
        branch_options = {f"{r['name']} ({r['code']})": r["code"] for r in branch_rows}
        branch_labels = st.multiselect(
            "Branches to run on", list(branch_options.keys()),
            default=list(branch_options.keys()),
        )

        draft_copy = st.text_area(
            "Draft copy (optional, English)",
            placeholder="Leave empty to let the Director Agent generate it.",
            height=120,
        )
        notes = st.text_area("Internal notes (optional)", height=60)

        submitted = st.form_submit_button("Save campaign (status: pending approval)")

    if submitted:
        if not title or not poster or not brands or not branch_labels:
            st.error("Title, poster, at least one brand and one branch are required.")
            return

        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        ext = Path(poster.name).suffix.lower() or ".png"
        safe = "".join(c if c.isalnum() else "_" for c in title)[:40]
        poster_path = UPLOADS / f"{ts}_{safe}{ext}"
        poster_path.write_bytes(poster.read())

        branch_codes = [branch_options[lbl] for lbl in branch_labels]

        with db() as conn:
            cur = conn.execute(
                """INSERT INTO campaigns
                   (title, poster_path, brands, branch_codes,
                    draft_copy, status, created_at, notes)
                   VALUES (?, ?, ?, ?, ?, 'pending_approval', ?, ?)""",
                (
                    title,
                    str(poster_path.relative_to(ROOT)),
                    json.dumps(brands),
                    json.dumps(branch_codes),
                    draft_copy or None,
                    datetime.utcnow().isoformat(),
                    notes or None,
                ),
            )
        st.success(f"Campaign #{cur.lastrowid} saved. Director Agent will draft platform copy on next run.")


def _campaign_click_summary(campaign_id: int):
    """Return aggregated click stats for a campaign (total + per-variant)."""
    now = datetime.utcnow()
    cutoffs = {
        "24h": (now - timedelta(hours=24)).isoformat(),
        "7d":  (now - timedelta(days=7)).isoformat(),
        "30d": (now - timedelta(days=30)).isoformat(),
    }
    with db() as conn:
        totals = {}
        for k, since in cutoffs.items():
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM click_logs WHERE campaign_id = ? AND clicked_at >= ?",
                (campaign_id, since),
            ).fetchone()
            totals[k] = row["n"] if row else 0
        # per-variant clicks via utm_campaign matching
        per_variant = conn.execute(
            """SELECT utm_campaign, COUNT(*) AS clicks, MAX(clicked_at) AS last_click
               FROM click_logs WHERE campaign_id = ?
               GROUP BY utm_campaign ORDER BY clicks DESC""",
            (campaign_id,),
        ).fetchall()
    return totals, per_variant


def _variant_clicks(campaign_id: int, utm_campaign: str):
    """Click count + last click timestamp for one variant."""
    with db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n, MAX(clicked_at) AS last_at
               FROM click_logs WHERE campaign_id = ? AND utm_campaign = ?""",
            (campaign_id, utm_campaign),
        ).fetchone()
    return (row["n"] if row else 0, row["last_at"] if row else None)


def _utm_from_landing_url(url: str) -> str:
    """Extract utm_campaign from a landing URL string."""
    if not url or "utm_campaign=" not in url:
        return ""
    tail = url.split("utm_campaign=", 1)[1]
    return tail.split("&", 1)[0]


def page_campaigns():
    st.header("📋 Campaigns")
    status_filter = st.selectbox(
        "Status", ["all", "pending_approval", "approved", "live", "archived", "draft"]
    )
    with db() as conn:
        q = "SELECT * FROM campaigns"
        params: tuple = ()
        if status_filter != "all":
            q += " WHERE status = ?"
            params = (status_filter,)
        q += " ORDER BY created_at DESC LIMIT 100"
        rows = conn.execute(q, params).fetchall()

    if not rows:
        st.info("No campaigns yet.")
        return

    for r in rows:
        # Pre-compute lightweight stats so the expander label can show live count
        with db() as conn:
            live_count = conn.execute(
                """SELECT COUNT(*) AS n FROM ad_creatives
                   WHERE campaign_id = ? AND approved = 1 AND live_at IS NOT NULL
                     AND paused_at IS NULL""",
                (r["id"],),
            ).fetchone()["n"]
            approved_count = conn.execute(
                """SELECT COUNT(*) AS n FROM ad_creatives
                   WHERE campaign_id = ? AND approved = 1""",
                (r["id"],),
            ).fetchone()["n"]
        live_badge = (
            f" · 🟢 {live_count} live"
            if live_count
            else (f" · ✅ {approved_count} approved" if approved_count else "")
        )
        with st.expander(
            f"#{r['id']} · {r['title']} · {r['status']}{live_badge}"
        ):
            cols = st.columns([1, 2])
            with cols[0]:
                poster = ROOT / r["poster_path"]
                if poster.exists():
                    st.image(str(poster), use_container_width=True)
            with cols[1]:
                st.write(f"**Brands:** {', '.join(json.loads(r['brands']))}")
                st.write(f"**Branches:** {', '.join(json.loads(r['branch_codes']))}")
                st.write(f"**Created:** {r['created_at']}")
                if r["draft_copy"]:
                    st.write("**Draft copy:**")
                    st.code(r["draft_copy"])
                if r["notes"]:
                    st.caption(f"Notes: {r['notes']}")

                action_cols = st.columns(4)
                with action_cols[0]:
                    if st.button("Approve", key=f"a{r['id']}"):
                        with db() as conn:
                            conn.execute(
                                "UPDATE campaigns SET status='approved', approved_at=? WHERE id=?",
                                (datetime.utcnow().isoformat(), r["id"]),
                            )
                        st.rerun()
                with action_cols[1]:
                    if st.button("Mark Live", key=f"l{r['id']}"):
                        with db() as conn:
                            conn.execute(
                                "UPDATE campaigns SET status='live' WHERE id=?",
                                (r["id"],),
                            )
                        st.rerun()
                with action_cols[2]:
                    if st.button("Archive", key=f"x{r['id']}"):
                        with db() as conn:
                            conn.execute(
                                "UPDATE campaigns SET status='archived' WHERE id=?",
                                (r["id"],),
                            )
                        st.rerun()
                with action_cols[3]:
                    if st.button("🪄 Generate Copy", key=f"g{r['id']}",
                                 help="Director Agent generates 5 English ad variants"):
                        try:
                            with st.spinner("Director is writing ad copy..."):
                                n = generate_and_save(r["id"])
                            st.success(f"Saved {n} variants. Scroll to **Ad Creatives** below.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Generation failed: {e}")

            # Live performance summary (only if any creatives exist)
            with db() as conn:
                creatives = conn.execute(
                    """SELECT * FROM ad_creatives
                       WHERE campaign_id = ?
                       ORDER BY
                         CASE WHEN approved = 1 AND live_at IS NOT NULL AND paused_at IS NULL THEN 0
                              WHEN approved = 1 THEN 1
                              WHEN rejected_at IS NULL THEN 2
                              ELSE 3 END,
                         created_at DESC""",
                    (r["id"],),
                ).fetchall()

            if creatives:
                totals, per_variant = _campaign_click_summary(r["id"])
                st.markdown("---")
                st.markdown("### 📈 Live Performance")
                m = st.columns(4)
                m[0].metric("Approved variants", approved_count)
                m[1].metric("Live now", live_count)
                m[2].metric("Clicks (24 h)", totals["24h"])
                m[3].metric("Clicks (7 d)", totals["7d"])
                if totals["7d"] == 0:
                    st.caption(
                        "No clicks tracked yet. Once a variant is **live** on Meta and "
                        "the ad runs with the variant's landing URL, redirects show up here."
                    )

                st.markdown("---")
                st.markdown("### 🎨 Ad Creatives")
                for c in creatives:
                    if c["approved"] and c["live_at"] and not c["paused_at"]:
                        badge = "🟢 LIVE"
                    elif c["paused_at"]:
                        badge = "⏸ Paused"
                    elif c["approved"]:
                        badge = "✅ Approved"
                    elif c["rejected_at"]:
                        badge = "❌ Rejected"
                    else:
                        badge = "⏳ Pending"

                    utm = _utm_from_landing_url(c["landing_url"] or "")
                    v_clicks, v_last = _variant_clicks(r["id"], utm)

                    platforms = json.loads(c["platforms"] or "[]") or ["meta_fb", "meta_ig"]
                    platforms_str = ", ".join(
                        {"meta_fb": "Facebook", "meta_ig": "Instagram",
                         "google": "Google", "tiktok": "TikTok"}.get(p, p)
                        for p in platforms
                    )

                    st.markdown(
                        f"**Variant #{c['id']}** · {badge}  ·  "
                        f"audience `{c['audience']}` · angle `{c['angle']}`"
                    )

                    pcols = st.columns([1, 1, 1])
                    with pcols[0]:
                        # AI-generated poster preview (if exists)
                        if c["asset_path"]:
                            poster_path = ROOT / c["asset_path"]
                            if poster_path.exists():
                                st.image(str(poster_path), use_container_width=True)
                            else:
                                st.caption("Poster file missing on disk")
                        else:
                            st.caption("No poster yet")
                            if poster_enabled():
                                if st.button(
                                    f"🎨 Generate Poster ({_poster_cost_label()})",
                                    key=f"ac_poster{c['id']}",
                                    help="AI generates a unique poster image for this variant"
                                ):
                                    try:
                                        with st.spinner("Generating poster (30-60 sec at high quality)..."):
                                            path = generate_poster_for_variant(c["id"])
                                        if path:
                                            st.success("Poster ready ✅")
                                            st.rerun()
                                        else:
                                            st.error("Generation returned empty — check logs")
                                    except Exception as e:
                                        st.error(f"Failed: {e}")
                            else:
                                st.caption("OPENAI_API_KEY not set — poster gen disabled")
                    with pcols[1]:
                        # Ad copy preview card — explicit colors so it works in both
                        # light and dark Streamlit themes
                        st.markdown(
                            f"""<div style='border:1px solid #e5e7eb;border-radius:8px;padding:12px;background:#ffffff;height:100%;color:#1f2937;'>
<div style='font-size:12px;color:#6b7280;margin-bottom:6px;'>Sponsored · PARTS-MALL</div>
<div style='font-weight:700;font-size:16px;margin-bottom:6px;color:#1e3a8a;'>{(c['headline'] or '(no headline)').replace('<','&lt;')}</div>
<div style='font-size:14px;margin-bottom:10px;color:#1f2937;'>{(c['primary_text'] or '').replace('<','&lt;')}</div>
<div style='display:inline-block;padding:6px 14px;background:#25D366;color:#ffffff;border-radius:6px;font-weight:600;font-size:13px;'>{c['cta'] or 'Send WhatsApp'}</div>
</div>""",
                            unsafe_allow_html=True,
                        )
                    with pcols[2]:
                        st.metric("Clicks", v_clicks)
                        if v_last:
                            st.caption(f"Last click: {v_last[:19]} UTC")
                        else:
                            st.caption("No clicks yet")
                        st.caption(f"On: **{platforms_str}**")
                        if c["asset_path"] and poster_enabled():
                            if st.button(
                                f"🔄 Regenerate Poster ({_poster_cost_label()})",
                                key=f"ac_repost{c['id']}",
                                help="Replace current poster with a new AI generation"
                            ):
                                try:
                                    with st.spinner("Regenerating (30-60 sec)..."):
                                        path = generate_poster_for_variant(c["id"])
                                    if path:
                                        st.success("New poster ready")
                                        st.rerun()
                                except Exception as e:
                                    st.error(f"Failed: {e}")

                    st.caption(
                        f"Tags: {', '.join(json.loads(c['hashtags'] or '[]'))}  ·  "
                        f"CTA URL ↓"
                    )
                    st.code(c["landing_url"] or "", language=None)

                    # Action buttons
                    btns = st.columns(5)
                    is_live = bool(c["approved"] and c["live_at"] and not c["paused_at"])
                    is_approved = bool(c["approved"])

                    with btns[0]:
                        if not is_approved and st.button("Approve", key=f"ac_a{c['id']}"):
                            with db() as conn:
                                conn.execute(
                                    """UPDATE ad_creatives
                                       SET approved=1, approved_at=?, rejected_at=NULL
                                       WHERE id=?""",
                                    (datetime.utcnow().isoformat(), c["id"]),
                                )
                            st.rerun()
                    with btns[1]:
                        if is_approved and not is_live and st.button(
                            "🟢 Mark Live", key=f"ac_live{c['id']}",
                            help="Set this when you've posted the ad on Meta"
                        ):
                            with db() as conn:
                                conn.execute(
                                    """UPDATE ad_creatives
                                       SET live_at=?, paused_at=NULL
                                       WHERE id=?""",
                                    (datetime.utcnow().isoformat(), c["id"]),
                                )
                            st.rerun()
                    with btns[2]:
                        if is_live and st.button(
                            "⏸ Pause", key=f"ac_pause{c['id']}",
                            help="Manual pause — set when you pause it on Meta"
                        ):
                            with db() as conn:
                                conn.execute(
                                    "UPDATE ad_creatives SET paused_at=? WHERE id=?",
                                    (datetime.utcnow().isoformat(), c["id"]),
                                )
                            st.rerun()
                    with btns[3]:
                        if not c["rejected_at"] and st.button("Reject", key=f"ac_r{c['id']}"):
                            with db() as conn:
                                conn.execute(
                                    """UPDATE ad_creatives
                                       SET approved=0, rejected_at=?, live_at=NULL, paused_at=NULL
                                       WHERE id=?""",
                                    (datetime.utcnow().isoformat(), c["id"]),
                                )
                            st.rerun()
                    with btns[4]:
                        if st.button("Delete", key=f"ac_d{c['id']}"):
                            with db() as conn:
                                conn.execute(
                                    "DELETE FROM ad_creatives WHERE id=?", (c["id"],)
                                )
                            st.rerun()
                    st.markdown("---")


def page_staff():
    st.header("👥 Staff & WhatsApp Routing")
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, b.name AS branch_name, b.code AS branch_code
               FROM staff s JOIN branches b ON b.id = s.branch_id
               ORDER BY b.name, s.name"""
        ).fetchall()

    for r in rows:
        cols = st.columns([2, 2, 1, 1, 1])
        cols[0].write(f"**{r['branch_name']}** — {r['name']}")
        cols[1].code(r["whatsapp_e164"])
        cols[2].write(r["number_type"])
        active = cols[3].checkbox("Active", value=bool(r["active"]), key=f"act{r['id']}")
        if active != bool(r["active"]):
            with db() as conn:
                conn.execute("UPDATE staff SET active=? WHERE id=?", (int(active), r["id"]))
            st.rerun()
        cols[4].caption(f"Last: {r['last_assigned_at'] or '—'}")


def page_analytics():
    st.header("📊 Click Analytics")
    days = st.slider("Last N days", 1, 30, 7)
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    with db() as conn:
        by_branch = conn.execute(
            """SELECT branch_code, COUNT(*) AS clicks
               FROM click_logs WHERE clicked_at >= ?
               GROUP BY branch_code ORDER BY clicks DESC""",
            (since,),
        ).fetchall()
        by_brand = conn.execute(
            """SELECT brand, COUNT(*) AS clicks
               FROM click_logs WHERE clicked_at >= ?
               GROUP BY brand ORDER BY clicks DESC""",
            (since,),
        ).fetchall()
        by_staff = conn.execute(
            """SELECT s.name, b.name AS branch, COUNT(*) AS clicks
               FROM click_logs c
               JOIN staff s ON s.id = c.staff_id
               JOIN branches b ON b.id = s.branch_id
               WHERE c.clicked_at >= ?
               GROUP BY s.id ORDER BY clicks DESC""",
            (since,),
        ).fetchall()

    cols = st.columns(3)
    with cols[0]:
        st.subheader("By branch")
        st.table([dict(r) for r in by_branch] or [{"info": "no data"}])
    with cols[1]:
        st.subheader("By brand")
        st.table([dict(r) for r in by_brand] or [{"info": "no data"}])
    with cols[2]:
        st.subheader("By staff (load balance)")
        st.table([dict(r) for r in by_staff] or [{"info": "no data"}])


# --- main ---
def main():
    init_db()
    if not check_password():
        st.stop()

    st.sidebar.image(str(ROOT / "assets" / "logo.png"), use_container_width=True)
    st.sidebar.title("PARTS-MALL Admin")
    st.sidebar.caption("Boksburg + Edenvale MVP")

    if st.sidebar.button("Re-seed branches/staff"):
        seed()
        st.sidebar.success("Seeded.")

    page = st.sidebar.radio(
        "Section",
        ["Upload Campaign", "Campaigns", "Staff", "Analytics"],
    )

    if page == "Upload Campaign":
        page_upload()
    elif page == "Campaigns":
        page_campaigns()
    elif page == "Staff":
        page_staff()
    elif page == "Analytics":
        page_analytics()


if __name__ == "__main__":
    main()
