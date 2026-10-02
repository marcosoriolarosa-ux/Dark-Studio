@echo off
REM ===========================================================================
REM  Dark Video Studio - inicio do servidor (Windows 10/11)
REM
REM  Ficheiro ASCII puro, com quebras CRLF e sem BOM, de proposito: o cmd.exe
REM  le o ficheiro byte a byte na pagina de codigo corrente, e um acento ou uma
REM  BOM aqui parte o script. O UTF-8 fica garantido por PYTHONUTF8 e
REM  PYTHONIOENCODING; o HTML ja e servido com charset UTF-8.
REM
REM  Porta: DARK_STUDIO_PORT (predefinicao 8013), procura a primeira livre ate
REM  DARK_STUDIO_PORT_MAX. Para nao abrir o navegador: DARK_STUDIO_NO_BROWSER=1
REM
REM  Nao ha execucao retardada (EnableDelayedExpansion) de proposito: um "!" no
REM  caminho do projeto corromperia o texto ao expandi-lo.
REM ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Dark Video Studio - Servidor

REM --- Entrar na pasta do projeto. daqui em diante so caminhos RELATIVOS: e o
REM     que resta o comando dentro do for /f, onde um & no caminho seria lido
REM     como separador de comandos. As aspas protegem acentos, espacos e &.
set "USED_PUSHD="
cd /d "%~dp0"
if not errorlevel 1 goto :dir_ok
REM cd /d nao aceita caminhos UNC; o pushd mapeia uma letra de drive temporaria.
pushd "%~dp0"
if errorlevel 1 goto :dir_fail
set "USED_PUSHD=1"
:dir_ok
goto :dir_ready
:dir_fail
echo.
echo  [ERRO] Nao foi possivel entrar na pasta do projeto:
echo         %~dp0
echo  Se for uma unidade de rede, copie o projeto para o disco local e repita.
echo.
pause
exit /b 1
:dir_ready
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if defined PYTHONPATH (
    set "PYTHONPATH=%CD%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%CD%"
)

echo.
echo  ============================================
echo    DARK VIDEO STUDIO - Iniciar Servidor
echo  ============================================
echo.
echo  Projeto: %CD%

REM --- 1. Interpretador Python ---------------------------------------------
REM Ordem: .venv do projeto, depois py -3, depois python. PYEXE e sempre um
REM caminho (nunca "py -3", que dentro de aspas seria um executable inexistente);
REM os argumentos extra vao em PYARGS. Um Python de sistema nunca e usado em
REM silencio: o passo 2 valida os pacotes e aborta se faltarem.
echo  [1/4] A procurar o Python...
set "PYEXE="
set "PYARGS="
REM Um .venv a meio (criacao interrompida) existe mas nao executa: testamos
REM antes de o usar, senao o erro aparece como um traceback mais a frente.
if exist "%CD%\.venv\Scripts\python.exe" (
    "%CD%\.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYEXE=%CD%\.venv\Scripts\python.exe"
)
if defined PYEXE goto :python_ok
if exist "%CD%\.venv\Scripts\python.exe" echo        AVISO: o .venv existe mas nao executa. Se for o caso, apague-o e refaca: python -m venv .venv
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if defined PYEXE set "PYARGS=-3"
if defined PYEXE goto :python_ok
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
:python_ok
if not defined PYEXE goto :no_python
echo        Usando: %PYEXE% %PYARGS%
goto :python_ready
:no_python
echo.
echo  [ERRO] Python nao encontrado. Este projeto precisa de Python 3.10 ou superior.
echo         Instale Python 3.10 ou superior: https://www.python.org/downloads/
echo         e marque a opcao "Add python.exe to PATH" no instalador.
echo.
pause
exit /b 1
:python_ready

REM --- 2. Verificacao de ambiente ------------------------------------------
REM O mesmo verificador em Windows, Linux e macOS, para as mensagens serem
REM identicas. Sai com codigo 1 se faltar algo obrigatorio.
echo  [2/4] A verificar dependencias e ferramentas...
"%PYEXE%" %PYARGS% "scripts\check_env.py" --fix
if errorlevel 1 (
    echo.
    echo  [ERRO] O ambiente nao esta pronto. Corrija o que ficou em falta acima.
    echo         Se ainda nao existe um .venv com as dependencias:
    echo             python -m venv .venv
    echo             .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM --- 3. Porta ------------------------------------------------------------
REM A sonda e um bind() em Python sem SO_REUSEADDR, e nao texto do netstat: o
REM estado "LISTENING" aparece traduzido em algumas versoes do Windows, o que
REM daria um falso "porta livre". O netstat abaixo e so diagnostico, para mostrar
REM quem e que esta a ocupar a porta.
set "BASE_PORT=%DARK_STUDIO_PORT%"
if not defined BASE_PORT set "BASE_PORT=8013"
set "PORT="
for /f "usebackq tokens=1,* delims==" %%A in (`"%PYEXE%" %PYARGS% "scripts\check_env.py" --pick-port --machine %BASE_PORT%`) do (
    if /I "%%A"=="PORT" set "PORT=%%B"
)
if not defined PORT goto :no_port
if not "%PORT%"=="%BASE_PORT%" (
    echo  [3/4] Porta %BASE_PORT% ocupada. Quem esta a escutar ^(PID na ultima coluna^):
    netstat -ano -p tcp ^| findstr /R /C:":%BASE_PORT% "
    echo        A usar a porta %PORT% em vez disso.
) else (
    echo  [3/4] Porta %PORT% livre.
)
goto :server
:no_port
echo.
echo  [ERRO] Nenhuma porta livre a partir de %BASE_PORT%.
echo         Feche outros servidores ou defina outra base, por exemplo:
echo             set DARK_STUDIO_PORT=9000
echo.
pause
exit /b 1

REM --- 4. Servidor ---------------------------------------------------------
:server
echo.
echo  [4/4] A iniciar o servidor...
echo.
echo    Servidor:     http://127.0.0.1:%PORT%
echo    Interface:    http://127.0.0.1:%PORT%/app/
echo    Documentacao: http://127.0.0.1:%PORT%/docs
echo.
echo    Para encerrar: Ctrl+C
echo.

REM --- Navegador, 4 s depois, para o servidor ja estar a responder -----------
REM Fica fora de qualquer bloco: o codigo Python tem parenteses, e parenteses
REM literais dentro de um bloco fecham-no mais cedo.
if defined DARK_STUDIO_NO_BROWSER goto :no_browser
start "" /b "%PYEXE%" %PYARGS% -c "import time,webbrowser; time.sleep(4); webbrowser.open('http://127.0.0.1:%PORT%/app/')"
:no_browser

"%PYEXE%" %PYARGS% -m uvicorn backend.app:app --host 127.0.0.1 --port %PORT%
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo  [ERRO] O servidor terminou com codigo %EXITCODE%.
    echo         A causa esta acima. Para diagnostico completo:
    echo             "%PYEXE%" %PYARGS% "scripts\check_env.py"
    echo.
    pause
)
if "%USED_PUSHD%"=="1" popd
endlocal & exit /b %EXITCODE%
