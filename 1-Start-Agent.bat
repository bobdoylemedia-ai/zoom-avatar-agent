@echo off
title Zoom Avatar Agent - Worker (close this window, or Ctrl+C, to stop)
cd /d "%~dp0"
echo ==========================================================
echo   Zoom Avatar Agent - Worker
echo ==========================================================
echo.
echo Starting up. Wait for the line that says "registered worker".
echo.
echo Then LEAVE THIS WINDOW OPEN and run 2-Send-To-Meeting.bat.
echo.
echo To stop: close this window, or press Ctrl+C.
echo.
call uv run python src/agent.py dev
echo.
echo Worker stopped.
pause
