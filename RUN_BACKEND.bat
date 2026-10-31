@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.11 or 3.12 first.
  pause
  exit /b 1
)
if not exist ".env" (
  echo Configure .env before running the backend.
  copy ".env.example" ".env"
  start "" notepad ".env"
  pause
  exit /b 1
)
py -c "import flask, requests, dotenv, cv2, numpy" >nul 2>&1
if errorlevel 1 (
  echo Installing required Python libraries... Requires internet.
  py -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Read README.md.
    pause
    exit /b 1
  )
)
echo Starting SafeGuard AI backend and Telegram supervisor bot...
py m.py
pause
