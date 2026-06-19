@echo off
chcp 65001 >nul 2>&1
title OpenCode Proxy

REM --- Vérifier si OpenVPN est installé ---
where openvpn >nul 2>&1
if errorlevel 1 (
    if exist "C:\Program Files\OpenVPN\bin\openvpn.exe" (
        set PATH=%PATH%;C:\Program Files\OpenVPN\bin
    ) else if exist "C:\Program Files (x86)\OpenVPN\bin\openvpn.exe" (
        set PATH=%PATH%;C:\Program Files (x86)\OpenVPN\bin
    )
)

REM --- Lancer le proxy ---
python opencode.py
