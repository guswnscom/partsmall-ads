"""FastAPI landing app — customer-facing.

Routes:
  GET  /                       -> region picker (Boksburg / Edenvale)
  GET  /healthz                -> liveness check
  GET  /photo/{token}          -> serve uploaded customer photo (license disc, part)
  GET  /{branch_code}          -> region landing page (mobile-first)
  POST /go/{branch_code}       -> log click + redirect to wa.me

영업시간 외 클릭에도 항상 wa.me로 리다이렉트하되,
prefill 메시지에 "다음 영업일에 답변" 안내를 포함 (routing.py).
"""

from __future__ import annotations
import json
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# load .env so ANTHROPIC_API_KEY etc. are available
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from core.db import db, init_db
from core.ocr import extract_vin_from_image
from core.routing import pick_staff, build_whatsapp_url, is_open

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
CUSTOMER_UPLOADS = ROOT_DIR / "uploads" / "customer"
CUSTOMER_UPLOADS.mkdir(parents=True, exist_ok=True)

# Public base URL for photo links sent over WhatsApp.
# In dev, defaults to http://127.0.0.1:8000 — staff phones can't reach localhost.
# In prod, set LANDING_BASE_URL=https://psms-pmaad.co.za in .env.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

app = FastAPI(title="PARTS-MALL Landing")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/assets", StaticFiles(directory=str(ROOT_DIR / "assets")), name="assets")


PRIORITY_BRANDS = ["Hyundai", "Kia", "Chevrolet", "Suzuki", "Ssangyong"]
OTHER_BRANDS = ["Other"]


def _public_base(request: Request) -> str:
    return os.getenv("LANDING_BASE_URL") or DEFAULT_BASE_URL


async def _save_upload(upload: Optional[UploadFile], label: str) -> Optional[str]:
    """Save an UploadFile and return its public token (filename)."""
    if upload is None or not upload.filename:
        return None
    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return None
    body = await upload.read()
    if not body or len(body) > MAX_UPLOAD_BYTES:
        return None
    token = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{label}_{secrets.token_hex(6)}{ext}"
    (CUSTOMER_UPLOADS / token).write_bytes(body)
    return token


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True, "open_now": is_open()}


@app.get("/photo/{token}")
def serve_photo(token: str):
    # prevent path traversal
    safe = Path(token).name
    fp = CUSTOMER_UPLOADS / safe
    if not fp.exists():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(fp)


@app.get("/asset/{filename}")
def serve_generated_asset(filename: str):
    """Serve AI-generated ad posters from /generated/ folder.
    Used in admin previews and as Meta ad image source."""
    safe = Path(filename).name
    fp = ROOT_DIR / "generated" / safe
    if not fp.exists():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(fp)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db() as conn:
        branches = conn.execute(
            "SELECT code, name, city, address, landline FROM branches WHERE active = 1 ORDER BY name"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"branches": branches, "is_open": is_open()},
    )


@app.get("/{branch_code}", response_class=HTMLResponse)
def branch_page(branch_code: str, request: Request):
    with db() as conn:
        branch = conn.execute(
            "SELECT * FROM branches WHERE code = ? AND active = 1",
            (branch_code,),
        ).fetchone()
    if not branch:
        return HTMLResponse("Branch not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "branch.html",
        {
            "branch": branch,
            "priority_brands": PRIORITY_BRANDS,
            "other_brands": OTHER_BRANDS,
            "is_open": is_open(),
        },
    )


@app.post("/go/{branch_code}")
async def go(
    branch_code: str,
    request: Request,
    brand: str = Form(...),
    part: str = Form(""),
    vin: str = Form(""),
    license_disc: Optional[UploadFile] = File(None),
    part_photo: Optional[UploadFile] = File(None),
    campaign_id: Optional[int] = Form(None),
    utm_source: Optional[str] = Form(None),
    utm_campaign: Optional[str] = Form(None),
):
    with db() as conn:
        branch = conn.execute(
            "SELECT * FROM branches WHERE code = ?", (branch_code,)
        ).fetchone()
        if not branch:
            return RedirectResponse("/", status_code=303)

    staff = pick_staff(branch_code)
    if not staff:
        return HTMLResponse(
            "No staff available right now. Please try again shortly.",
            status_code=503,
        )

    license_token = await _save_upload(license_disc, "disc")
    part_token = await _save_upload(part_photo, "part")

    # If customer didn't type VIN but uploaded a license disc, OCR it via Claude Vision.
    # If they typed one AND uploaded — still OCR for staff verification.
    extracted_vin: Optional[str] = None
    if license_token:
        extracted_vin = extract_vin_from_image(CUSTOMER_UPLOADS / license_token)

    base = _public_base(request)
    photo_urls: List[tuple[str, str]] = []
    if license_token:
        photo_urls.append(("License disc", f"{base}/photo/{license_token}"))
    if part_token:
        photo_urls.append(("Part photo", f"{base}/photo/{part_token}"))

    # log the click
    with db() as conn:
        conn.execute(
            """INSERT INTO click_logs
               (branch_code, staff_id, campaign_id, brand, part_query, vin,
                license_disc_path, part_photo_path, extracted_vin,
                user_agent, ip, referrer, utm_source, utm_campaign, clicked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                branch_code,
                staff["id"],
                campaign_id,
                brand,
                part,
                vin,
                license_token,
                part_token,
                extracted_vin,
                request.headers.get("user-agent", "")[:300],
                request.client.host if request.client else None,
                request.headers.get("referer", "")[:300],
                utm_source,
                utm_campaign,
                datetime.utcnow().isoformat(),
            ),
        )

    url = build_whatsapp_url(
        e164=staff["whatsapp_e164"],
        branch_name=branch["name"],
        brand=brand,
        part=part or None,
        vin=vin or None,
        extracted_vin=extracted_vin,
        photo_urls=photo_urls or None,
    )
    return RedirectResponse(url, status_code=303)
