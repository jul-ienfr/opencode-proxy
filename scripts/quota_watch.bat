@echo off
REM Veille prioritaire : detecte et capture le premier 200 du chat gratuit.
REM Tache "OpenCodeQuotaWatch" toutes les 10 minutes, 9 minutes de veille.
REM Le verrou .watch.lock empeche deux instances simultanees.
setlocal
cd /d "%~dp0.."
if not exist "logs\quota_reset_capture" mkdir "logs\quota_reset_capture"
python scripts\quota_reset_capture.py --watch --max-seconds 540 >> "logs\quota_reset_capture\watch.log" 2>&1
endlocal
