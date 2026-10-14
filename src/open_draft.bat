@echo off
rem Open the dictation draft (appends live, no need to reopen).
rem VS Code picks up external changes itself; classic Notepad asks about reload.
where code >nul 2>nul
if %errorlevel%==0 (
  start "" code --reuse-window "%~dp0draft.txt"
) else (
  start "" notepad "%~dp0draft.txt"
)
