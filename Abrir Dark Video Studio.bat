@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente Python nao encontrado em .venv.
  echo Execute primeiro os passos do README.md.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$api = Get-NetTCPConnection -LocalPort 8013 -State Listen -ErrorAction SilentlyContinue; if (-not $api) { Start-Process -FilePath '%CD%\.venv\Scripts\python.exe' -ArgumentList '-m uvicorn backend.app:app --host 127.0.0.1 --port 8013' -WorkingDirectory '%CD%' -WindowStyle Minimized }"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$web = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue; if (-not $web) { Start-Process -FilePath '%CD%\.venv\Scripts\python.exe' -ArgumentList '-m http.server 8080 --directory frontend' -WorkingDirectory '%CD%' -WindowStyle Minimized }"

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8080/?v=%RANDOM%"
exit /b 0
