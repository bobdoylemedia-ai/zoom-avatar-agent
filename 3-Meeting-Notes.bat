@echo off
title Zoom Avatar Agent - Meeting Notes
cd /d "%~dp0"
if not exist "meetings" (
  echo No meetings folder yet. Send the avatar into a meeting first.
  echo.
  pause
  exit /b 1
)
echo Opening your meeting notes. Newest files are at the top.
echo.
echo   .md    = the readable recap
echo   .jsonl = the full raw transcript
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Path 'meetings' -Filter *.md | Sort-Object LastWriteTime -Descending | Select-Object -First 10 Name, LastWriteTime | Format-Table -AutoSize"
start "" explorer.exe "%~dp0meetings"
pause
