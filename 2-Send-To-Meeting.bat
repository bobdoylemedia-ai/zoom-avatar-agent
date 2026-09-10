@echo off
title Zoom Avatar Agent - Send To Meeting
cd /d "%~dp0"
echo ==========================================================
echo   Send the avatar into a meeting
echo ==========================================================
echo.
echo The worker window (1-Start-Agent.bat) must already be running
echo and showing "registered worker".
echo.
echo Paste the meeting invite link below, then press Enter.
echo For Zoom, the link must include the passcode, like:
echo   https://us05web.zoom.us/j/1234567890?pwd=abc123
echo.
set "MEETING="
set /p MEETING=Meeting link: 
if not defined MEETING (
  echo.
  echo No link entered. Nothing sent.
  echo.
  pause
  exit /b 1
)
echo.
set "BOTNAME="
set /p BOTNAME=Display name in the meeting [Avatar (AI)]: 
if not defined BOTNAME set "BOTNAME=Avatar (AI)"
echo.
echo Which preset? Presets live in presets.json in this folder.
echo Leave this blank to use the defaults with no knowledge base.
echo.
set "WHICH="
set /p WHICH=Preset name (blank for none): 
echo.
set "GATE="
set /p GATE=Only speak when spoken to? Recommended for group calls (y/N): 
set "GATEFLAG="
if /i "%GATE%"=="y" set "GATEFLAG=--require-address"

if not defined WHICH (
  call uv run python src/send_to_meeting.py "%MEETING%" --bot-name "%BOTNAME%" %GATEFLAG%
) else (
  call uv run python src/send_to_meeting.py "%MEETING%" --bot-name "%BOTNAME%" --preset "%WHICH%" %GATEFLAG%
)
echo.
pause
