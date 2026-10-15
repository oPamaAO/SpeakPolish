@echo off
rem Voice input for opencode - uninstaller.
choice /C YN /M "Remove voice input (files, shortcut, autostart)"
if errorlevel 2 exit /b 0
set VDIR=%USERPROFILE%\.config\opencode\voice
rem Backup YOUR data first (drafts, history, recordings) - uninstall must not eat them:
for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set BAK=%%D
set BDIR=%USERPROFILE%\VoiceOpencode-backup-%BAK%
mkdir "%BDIR%" 2>nul
copy /y "%VDIR%\draft*.txt" "%BDIR%\" >nul 2>nul
xcopy /e /i /y "%VDIR%\history" "%BDIR%\history\" >nul 2>nul
xcopy /e /i /y "%VDIR%\recordings" "%BDIR%\recordings\" >nul 2>nul
echo Your texts saved to: "%BDIR%"
rmdir /s /q "%VDIR%" 2>nul
del /q "%USERPROFILE%\.config\opencode\agents\prompt-polisher.md" 2>nul
del /q "%USERPROFILE%\.config\opencode\commands\polish.md" 2>nul
del /q "%USERPROFILE%\.config\opencode\command\polish.md" 2>nul
del /q "%USERPROFILE%\Desktop\VoiceOpencode.lnk" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'VoiceOpencode.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'VoiceOpencode.lnk') -Force -ErrorAction SilentlyContinue"
del /q "%USERPROFILE%\Desktop\Golosovoi vvod opencode.lnk" 2>nul
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VoiceOpencode.lnk" 2>nul
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\voice-opencode.lnk" 2>nul
echo Removed. Python libraries and the model stay (shared with the system).
pause
