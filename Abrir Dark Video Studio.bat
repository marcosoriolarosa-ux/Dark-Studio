@echo off
REM ===========================================================================
REM  Atalho de arranque. Nao duplica logica: delega para start.bat, que e quem
REM  escolhe a porta, valida o ambiente e arranca o servidor. Assim esta janela
REM  e a unica que ve os logs do servidor (Ctrl+C para encerrar).
REM ===========================================================================
setlocal
cd /d "%~dp0"
call "%~dp0start.bat"
exit /b %ERRORLEVEL%
