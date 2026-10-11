@echo off
title Refresh report
cd /d "%~dp0"

echo.
echo   Refresh report
echo   --------------
echo   Reading new hands and rewriting report.html.
echo.

python -m pokertracker.cli refresh
if errorlevel 1 (
  echo.
  echo   Something went wrong - see the message above.
  pause
  exit /b 1
)

start "" "report.html"
