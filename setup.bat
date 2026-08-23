@echo off
chcp 65001 >nul 2>&1
title OpenCode Proxy - Setup automatique

echo ========================================
echo   OpenCode Proxy - Installation auto
echo ========================================
echo.

REM --- Vérifier Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installé.
    echo Telechargez Python: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python detecte

REM --- Installer les dépendances Python ---
echo.
echo Installation des dependances Python...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERREUR] Echec installation dependances
    pause
    exit /b 1
)
echo [OK] Dependances Python installees

REM --- Vérifier/Créer config ---
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo [OK] Fichier .env cree (a configurer)
    )
)

REM --- Créer dossiers ---
if not exist logs mkdir logs
if not exist vpn_configs mkdir vpn_configs

REM --- Lancer le proxy ---
echo.
echo ========================================
echo   Lancement du proxy...
echo   API + Dashboard: http://localhost:4000
echo ========================================
echo.
python opencode.py
