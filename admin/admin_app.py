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
        with st.expander(f"#{r['id']} · {r['title']} · {r['status']}"):
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

                action_cols = st.columns(3)
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
