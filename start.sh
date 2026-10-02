#!/bin/sh
# ===========================================================================
#  Dark Video Studio - inicio do servidor (Linux / macOS; e invocado como
#  `sh start.sh`, por isso nada de bashisms: sem [[ ]], sem arrays, sem &>).
#
#  Porta: DARK_STUDIO_PORT (predefinicao 8013), procura a primeira livre ate
#  DARK_STUDIO_PORT_MAX. Para nao abrir o navegador: DARK_STUDIO_NO_BROWSER=1
#  A logica e a mesma de start.bat: mesmo verificador, mesma sonda de portas.
# ===========================================================================
set -e

PROJECT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$PROJECT_DIR"

# O que o Python escreve na consola tem de ser UTF-8, mesmo com o terminal ou
# o locale em latin1: sem isto, um acento numa mensagem mata o processo.
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
export PYTHONUTF8 PYTHONIOENCODING
PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

echo ""
echo "  ============================================"
echo "    DARK VIDEO STUDIO - Iniciar Servidor"
echo "  ============================================"
echo ""
echo "  Projeto: $PROJECT_DIR"

# --- 1. Interpretador Python ---------------------------------------------
# Ordem: .venv do projeto, depois python3, depois python.
echo "  [1/4] A procurar o Python..."
PY=""
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PY="$PROJECT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
fi
if [ -z "$PY" ]; then
    echo ""
    echo "  [ERRO] Python nao encontrado. Instale o Python 3.10+ e repita."
    echo ""
    exit 1
fi
echo "        Usando: $PY"

# --- 2. Verificacao de ambiente ------------------------------------------
echo "  [2/4] A verificar dependencias e ferramentas..."
if ! "$PY" "$PROJECT_DIR/scripts/check_env.py" --fix; then
    echo ""
    echo "  [ERRO] O ambiente nao esta pronto. Corrija o que ficou em falta acima."
    echo "         Se ainda nao existe um .venv com as dependencias:"
    echo "             python3 -m venv .venv"
    echo "             .venv/bin/python -m pip install -r requirements.txt"
    echo ""
    exit 1
fi

# --- 3. Porta -------------------------------------------------------------
# --machine devolve "PORT=8013"; o prefixo permite ignorar qualquer aviso
# inesperado que o Python escreva no meio.
set +e
PORT=$("$PY" "$PROJECT_DIR/scripts/check_env.py" --pick-port --machine)
RC=$?
set -e
PORT=${PORT#PORT=}
if [ "$RC" -ne 0 ] || [ -z "$PORT" ]; then
    echo ""
    echo "  [ERRO] Nenhuma porta livre a partir de ${DARK_STUDIO_PORT:-8013}."
    echo "         Feche outros servidores ou defina outra base, por exemplo:"
    echo "             DARK_STUDIO_PORT=9000 sh start.sh"
    echo ""
    exit 1
fi
echo "  [3/4] Porta escolhida: $PORT"

echo ""
echo "  [4/4] A iniciar o servidor..."
echo ""
echo "    Servidor:     http://127.0.0.1:$PORT"
echo "    Interface:    http://127.0.0.1:$PORT/app/"
echo "    Documentacao: http://127.0.0.1:$PORT/docs"
echo ""
echo "    Para encerrar: Ctrl+C"
echo ""

# --- Navegador, atras, para o servidor ja estar a responder ----------------
if [ -z "$DARK_STUDIO_NO_BROWSER" ]; then
    DARK_STUDIO_URL="http://127.0.0.1:$PORT/app/" "$PY" -c \
        'import os,time,webbrowser; time.sleep(4); webbrowser.open(os.environ["DARK_STUDIO_URL"])' \
        >/dev/null 2>&1 &
fi

exec "$PY" -m uvicorn backend.app:app --host 127.0.0.1 --port "$PORT"
