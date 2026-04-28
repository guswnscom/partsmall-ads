@echo off
REM Streamlit admin UI for managers
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
streamlit run admin\admin_app.py --server.port 8501
