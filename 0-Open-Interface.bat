@echo off
title Zoom Avatar Agent - Interface (close this window to shut it down)
cd /d "%~dp0"
echo ==========================================================
echo   Zoom Avatar Agent
echo ==========================================================
echo.
echo Starting the interface. Your browser should open by itself.
echo If it doesn't, go to:  http://127.0.0.1:8765
echo.
echo Everything is done in the browser window: start the agent,
echo pick the face, voice and knowledge base, paste the meeting
echo link, and hit Send.
echo.
echo Leave this window open while you use it.
echo.
call uv run python src/webui.py
echo.
echo Interface stopped.
pause
