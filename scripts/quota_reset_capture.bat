@echo off
REM Capture au reset annonce (00:00 UTC). Tache "OpenCodeQuotaResetCapture".
REM Demarre a 23:55 UTC, attend la frontiere puis veille 20 minutes.
setlocal
cd /d "%~dp0.."
if not exist "logs\quota_reset_capture" mkdir "logs\quota_reset_capture"
echo. >> "logs\quota_reset_capture\run.log"
echo ===== %DATE% %TIME% demarrage ===== >> "logs\quota_reset_capture\run.log"
python scripts\quota_reset_capture.py --until-reset --watch --max-seconds 1200 >> "logs\quota_reset_capture\run.log" 2>&1
echo ===== %DATE% %TIME% fin (code %ERRORLEVEL%) ===== >> "logs\quota_reset_capture\run.log"
endlocal
