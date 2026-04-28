@echo off
REM FastAPI customer-facing landing
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip setuptools wheel
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
python -m core.seed
echo.
echo =====================================================
echo  Landing server starting at http://127.0.0.1:8000
echo  Try:  http://127.0.0.1:8000/healthz
echo        http://127.0.0.1:8000/boksburg
echo        http://127.0.0.1:8000/edenvale
echo  Press CTRL+C to stop.
echo =====================================================
echo.
python -m uvicorn landing.main:app --host 127.0.0.1 --port 8000
