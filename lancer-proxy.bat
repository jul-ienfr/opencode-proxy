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
echo API: http://localhost:4000
echo Dashboard: http://localhost:8082
echo.
python opencode.py

pause
