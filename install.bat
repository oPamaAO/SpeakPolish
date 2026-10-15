@echo off
rem Voice input for opencode - installer.
rem Usage: double-click install.bat. Needs Windows, microphone, internet (~2 GB).
setlocal
cd /d "%~dp0"

echo === [1/6] Checking Python ===
python --version >nul 2>nul
if errorlevel 1 (
  echo Python not found. Trying winget...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (
    echo Failed. Install Python 3.12+ from https://www.python.org/downloads/ ^(tick Add to PATH^) and run again.
    pause
    exit /b 1
  )
)
python --version

echo === [2/6] Installing libraries ===
python -m pip install -r src\requirements.txt
if errorlevel 1 (
  echo Library install failed. Check internet and run again.
  pause
  exit /b 1
)

echo === [3/6] Downloading speech model (~1.5 GB, once) ===
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('Model OK')"
if errorlevel 1 (
  echo Model download failed. Check internet and run again.
  pause
  exit /b 1
)

echo === [4/6] Copying files to opencode config folder ===
set VDIR=%USERPROFILE%\.config\opencode\voice
if not exist "%VDIR%" mkdir "%VDIR%"
rem Backup user-tuned files before overwrite (reinstall keeps your dictionary/config):
if exist "%VDIR%\config.yaml" copy /y "%VDIR%\config.yaml" "%VDIR%\config.yaml.bak" >nul
if exist "%VDIR%\terminology.yaml" copy /y "%VDIR%\terminology.yaml" "%VDIR%\terminology.yaml.bak" >nul
copy /y src\voice.py "%VDIR%\" >nul
copy /y src\config.yaml "%VDIR%\" >nul
copy /y src\terminology.yaml "%VDIR%\" >nul
copy /y src\requirements.txt "%VDIR%\" >nul
copy /y src\open_draft.bat "%VDIR%\" >nul
copy /y src\start_voice.bat "%VDIR%\" >nul
copy /y src\README.md "%VDIR%\" >nul
if not exist "%USERPROFILE%\.config\opencode\agents" mkdir "%USERPROFILE%\.config\opencode\agents"
copy /y assets\agents\prompt-polisher.md "%USERPROFILE%\.config\opencode\agents\" >nul
if not exist "%USERPROFILE%\.config\opencode\commands" mkdir "%USERPROFILE%\.config\opencode\commands"
copy /y assets\commands\polish.md "%USERPROFILE%\.config\opencode\commands\" >nul
rem Compat: some setups read singular command\ instead of commands\ - put it there too:
if not exist "%USERPROFILE%\.config\opencode\command" mkdir "%USERPROFILE%\.config\opencode\command"
copy /y assets\commands\polish.md "%USERPROFILE%\.config\opencode\command\" >nul
echo Done: files are in place.

echo === [5/6] Shortcut and autostart ===
choice /C YN /M "Create desktop shortcut (opens draft + starts script)"
if errorlevel 2 goto no_shortcut
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\shortcut.ps1" -Target "%VDIR%\start_voice.bat" -WorkDir "%VDIR%" -DesktopLink "VoiceOpencode.lnk" -Desc "Voice input for opencode"
if errorlevel 1 (
  echo Shortcut FAILED, see error above. Continuing without it.
) else (
  echo Shortcut created.
)
:no_shortcut
choice /C YN /M "Add to Windows autostart"
if errorlevel 2 goto no_auto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\shortcut.ps1" -Target "%VDIR%\start_voice.bat" -WorkDir "%VDIR%" -StartupLink "VoiceOpencode.lnk" -Desc "Voice input for opencode" -WindowStyle 7
if errorlevel 1 (
  echo Autostart FAILED, see error above.
) else (
  echo Autostart ON. To disable, delete the shortcut (or Windows Settings - Apps - Startup).
)
:no_auto

echo === [6/6] Done ===
echo Start: ADMIN terminal - python "%VDIR%\voice.py"
echo ^(global hotkeys need administrator rights^)
echo Hotkeys: F9 record/stop, Shift+F9 continue, Ctrl+F9 restart, Ctrl+Shift+Space polish draft, F10 polish from clipboard.
echo Guide: "%VDIR%\README.md" ^(in Russian^). Uninstall: uninstall.bat
pause
