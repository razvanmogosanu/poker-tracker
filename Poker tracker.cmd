@echo off
title Poker tracker
cd /d "%~dp0"

echo.
echo   Poker tracker
echo   -------------
echo   The dashboard will open in your browser.
echo   Use the "Refresh hands" button there to pull in new hands.
echo.
echo   Keep this window open while you use it.
echo   Close it (or press Ctrl+C) to stop the tracker.
echo.

python -m pokertracker.cli serve
if errorlevel 1 (
  echo.
  echo   Something went wrong - see the message above.
  pause
)
