@echo off
chcp 65001 >nul
cd /d "%~dp0"
title bgScore - EXE Erstellung
echo ========================================================
echo   bgScore - Erstelle eigenständige EXE-Datei
echo ========================================================
echo.
echo Starte PyInstaller Build-Prozess...
echo.
python -m PyInstaller --noconfirm --onefile --console --name "bgScore" --clean ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-data "cert.pem;." ^
  --add-data "key.pem;." ^
  --distpath "output" app.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ========================================================
    echo   [ERFOLG] Die Datei wurde erfolgreich erstellt:
    echo   %~dp0output\bgScore.exe
    echo ========================================================
    echo.
    echo Sie koennen bgScore.exe nun direkt per Doppelklick starten.
) else (
    echo.
    echo ========================================================
    echo   [FEHLER] Beim Erstellen der EXE ist ein Fehler aufgetreten.
    echo ========================================================
)
echo.
pause
