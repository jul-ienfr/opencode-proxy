@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python est introuvable. Installe Python 3.11+ puis relance ce fichier.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo Fichier .env cree depuis .env.example.
        echo Pense a renseigner OPENCODE_API_KEY dans .env si necessaire.
        echo.
    )
)

python -c "import fastapi, uvicorn, httpx, rich, tiktoken" >nul 2>nul
if errorlevel 1 (
    echo Installation des dependances Python...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Echec de l'installation des dependances.
        pause
        exit /b 1
    )
)

echo Lancement du proxy OpenCode...
echo API + Dashboard: http://localhost:4000
echo.

REM -- Lancement silencieux : pythonw n'ouvre aucune fenetre MS-DOS,
REM    et les appels docker/curl/wsl internes utilisent CREATE_NO_WINDOW
REM    (config/settings.py, dashboard/api.py, vpn_manager.py).
REM    Si pythonw est absent, on retombe sur python (fenetre console).
where pythonw >nul 2>nul
if %errorlevel%==0 (
    echo   ^> pythonw detecte : lancement sans fenetre MS-DOS...
    start "" pythonw opencode.py
) else (
    python opencode.py
    pause
)
