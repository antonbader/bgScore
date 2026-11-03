@echo off
chcp 65001 >nul
title bgScore - Blasrohr Schiesszettel-Auswertung
echo ========================================================
echo   bgScore - Automatische Blasrohr-Schiesszettel Webapp
echo ========================================================
echo.
echo Starte lokalen Server auf Port 5000...
echo Offne http://localhost:5000 im Browser...
echo (Zum Verbinden mit dem Handy im WLAN auf "Handy-Link" oben rechts klicken)
echo.
start "" "http://localhost:5000"
python "%~dp0app.py"
pause
