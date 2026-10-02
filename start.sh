#!/usr/bin/env bash
set -e

echo ""
echo "  ============================================"
echo "    DARK VIDEO STUDIO - Iniciar Servidor"
echo "  ============================================"
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -d ".venv" ]; then
    echo "  [ERRO] Ambiente virtual nao encontrado."
    echo "  Execute: python3 -m venv .venv"
    echo "  Depois: source .venv/bin/activate && pip install -r requirements.txt"
    echo ""
    exit 1
fi

echo "  [1/3] A ativar ambiente virtual..."
source .venv/bin/activate

echo "  [2/3] A iniciar servidor uvicorn na porta 8013..."
echo ""
echo "  > Servidor: http://127.0.0.1:8013"
echo "  > Interface: http://127.0.0.1:8013/app/"
echo "  > Documentacao: http://127.0.0.1:8013/docs"
echo ""

echo "  [3/3] A abrir navegador..."
if command -v xdg-open &>/dev/null; then
    xdg-open "http://127.0.0.1:8013/app/" &>/dev/null &
elif command -v open &>/dev/null; then
    open "http://127.0.0.1:8013/app/" &>/dev/null &
else
    echo "  (Nao foi possivel abrir o navegador automaticamente)"
fi

echo ""
echo "  > Para encerrar: Ctrl+C"
echo ""

uvicorn backend.app:app --host 127.0.0.1 --port 8013
