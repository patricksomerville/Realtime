@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0\.."

if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "first=%%A"
    if not "!first:~0,1!"=="#" (
      if not "%%B"=="" set "%%A=%%B"
    )
  )
)

if "%OPENAI_API_KEY%"=="" (
  if exist "%USERPROFILE%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%USERPROFILE%\.env") do (
      set "first=%%A"
      if not "!first:~0,1!"=="#" (
        if not "%%B"=="" (
          if "%%A"=="OPENAI_API_KEY" set "%%A=%%B"
        )
      )
    )
  )
)

if "%OPENAI_API_KEY%"=="" (
  echo [WARN] OPENAI_API_KEY not set. realtime.py will attempt to load from .env files.
)

if "%VOICE_USE_LOCAL_LLM_COMMANDS%"=="" set "VOICE_USE_LOCAL_LLM_COMMANDS=1"
if "%VOICE_EXECUTE_LLM_COMMANDS%"=="" set "VOICE_EXECUTE_LLM_COMMANDS=0"

if not exist "runtime_logs" mkdir "runtime_logs"

echo [INFO] Launching Realtime stack...

start "Realtime Daemon" /B python -m realtime
start "Realtime Viewer" /B python realtime/viewer.py --port 3088 --no-open

timeout /t 2 >nul
start "" http://localhost:3088

echo [INFO] Realtime running at http://localhost:3088
echo [INFO] Press Ctrl+C in each window to stop.

exit /b 0
