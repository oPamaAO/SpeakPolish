@echo off
rem Voice input launcher: self-elevates (global hotkeys only work from an
rem elevated process when the target window, e.g. admin terminal, is elevated),
rem then opens the draft in VS Code and starts the script minimized.
net session >nul 2>nul
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
if not exist draft.txt type nul > draft.txt
where code >nul 2>nul
if %errorlevel%==0 start "" code --reuse-window "%~dp0draft.txt"
start "voice-opencode" /min python "%~dp0voice.py"
