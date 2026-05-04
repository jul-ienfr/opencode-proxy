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

python -c "import fastapi, uvicorn, httpx, rich, tiktoken, pystray, PIL, webview" >nul 2>nul
if errorlevel 1 (
    echo Installation des dependances Python...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Echec de l'installation des dependances.
        pause
        exit /b 1
    )
)

echo Lancement du proxy OpenCode (mode GUI)...
echo L'icone apparait dans la barre des taches.
echo.

start "" pythonw opencode.py --gui