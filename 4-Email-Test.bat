@echo off
title Zoom Avatar Agent - Email Test
cd /d "%~dp0"
echo ==========================================================
echo   Test emailing of meeting notes
echo ==========================================================
echo.
echo This sends one short test email to the address in .env.local
echo (NOTES_EMAIL_TO). Run it once, before you rely on notes arriving
echo after a real meeting.
echo.
echo If it says the password is missing or rejected, you need a Gmail
echo App Password -- not your normal Google password:
echo   https://myaccount.google.com/apppasswords
echo.
call uv run python src/mailer.py
echo.
pause
