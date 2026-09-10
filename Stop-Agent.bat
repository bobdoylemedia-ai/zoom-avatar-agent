@echo off
title Zoom Avatar Agent - Stop
cd /d "%~dp0"
echo Stopping the agent and making sure the meeting notes are written...
echo.
echo This can take up to a minute: it lets the agent summarize the meeting,
echo render the PDF and send the email before it exits. Please wait.
echo.
call uv run python src/stop_agent.py
echo.
pause
