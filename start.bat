@echo off
chcp 65001 >nul 2>&1
title Dark Video Studio - Servidor

echo.
echo  ============================================
echo    DARK VIDEO STUDIO - Iniciar Servidor
echo  ============================================
echo.

REM Verificar se o ambiente virtual existe
if not exist ".venv\Scripts\python.exe" (
    echo  [ERRO] Ambiente virtual nao encontrado.
    echo  Execute: python -m venv .venv
    echo  Depois: .venv\Scripts\activate ^&^& pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo  [1/3] A ativar ambiente virtual...
call .venv\Scripts\activate.bat

echo  [2/3] A iniciar servidor uvicorn na porta 8013...
echo.
echo  ^> Servidor: http://127.0.0.1:8013
echo  ^> Interface: http://127.0.0.1:8013/app/
echo  ^> Documentacao: http://127.0.0.1:8013/docs
echo.
echo  [3/3] A abrir navegador...
start "" "http://127.0.0.1:8013/app/"

echo.
echo  ^> Para encerrar: Ctrl+C
echo.

uvicorn backend.app:app --host 127.0.0.1 --port 8013
