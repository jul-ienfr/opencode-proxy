@echo off
REM Surveillance continue : detecte le moment ou le chat gratuit re-accepte.
REM Lance par la tache planifiee "OpenCodeQuotaWatch" toutes les 30 minutes.
setlocal
cd /d "%~dp0.."
if not exist "logs\quota_reset_capture" mkdir "logs\quota_reset_capture"
python scripts\quota_reset_capture.py --now 45 >> "logs\quota_reset_capture\watch.log" 2>&1
endlocal
