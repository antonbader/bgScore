@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo   Starte auto-py-to-exe mit bgScore-Konfiguration...
echo ========================================================
echo.
auto-py-to-exe --config "%~dp0auto-py-to-exe-config.json"
