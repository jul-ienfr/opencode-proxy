@echo off
REM Capture automatique au reset de quota opencode (00:00 UTC).
REM Lance par la tache planifiee "OpenCodeQuotaResetCapture" a 01:55 locale.
setlocal
cd /d "%~dp0.."
if not exist "logs\quota_reset_capture" mkdir "logs\quota_reset_capture"
echo. >> "logs\quota_reset_capture\run.log"
echo ===== %DATE% %TIME% demarrage ===== >> "logs\quota_reset_capture\run.log"
python scripts\quota_reset_capture.py --until-reset >> "logs\quota_reset_capture\run.log" 2>&1
echo ===== %DATE% %TIME% fin (code %ERRORLEVEL%) ===== >> "logs\quota_reset_capture\run.log"
endlocal
