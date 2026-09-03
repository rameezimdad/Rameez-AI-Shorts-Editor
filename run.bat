@echo off
REM Developed by Mohammad Rameez Imdad (Rameez Scripts)
REM WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
REM YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)
REM One-click launcher for Windows: venv + deps + assets + server + browser.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.11+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
where ffmpeg >nul 2>nul || (
  if exist "C:\ffmpeg\bin\ffmpeg.exe" (set "PATH=C:\ffmpeg\bin;%PATH%") else (echo ffmpeg not found. Install it ^(winget install Gyan.FFmpeg^) or put it in C:\ffmpeg\bin. & pause & exit /b 1)
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
call ".venv\Scripts\activate.bat"
pip install -q -r requirements.txt || (echo pip install failed & pause & exit /b 1)
if not exist ".env" copy ".env.example" ".env"
python scripts\setup_assets.py
echo.
echo  Opening http://127.0.0.1:8000  (close this window to stop the server)
echo.
start "" http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
