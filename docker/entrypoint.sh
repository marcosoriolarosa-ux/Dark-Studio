#!/bin/sh
# =============================================================================
#  Dark Studio - entrypoint do container
#
#  `sh`-compativel de proposito: o runtime e Debian slim com dash como /bin/sh.
#  Sem bashisms - nada de [[ ]], arrays nem $'...' - porque um bashism num
#  entrypoint falha no arranque do container, que e o pior momento possivel.
#
#  Este ficheiro e deliberadamente ASCII-only, como scripts/check_env.py: e o
#  verificador de ambiente, e nao pode ser a coisa que rebenta.
# =============================================================================
set -e

# Raiz da aplicacao dentro da imagem. PYTHONPATH=/app (no Dockerfile) e o que
# permite `import backend.app`; o cd garante que os caminhos que o codigo abre
# (storage/, .env) resolvem para /app e nao para a raiz do container, que e o
# default do Docker (/).
APP_DIR=/app
cd "$APP_DIR"

# As mesmas tres variaveis que o start.sh exporta no Windows. PYTHONUTF8 e
# PYTHONIOENCODING sao o que impede um UnicodeEncodeError no primeiro log com
# acento pt-PT num container com locale POSIX/C.
PYTHONUNBUFFERED=1
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
PYTHONPATH="$APP_DIR"
export PYTHONUNBUFFERED PYTHONUTF8 PYTHONIOENCODING PYTHONPATH

# O virtualenv do estagio builder foi copiado para /opt/venv. Sem isto no PATH o
# `python` seria o do sistema, que nao tem nenhuma dependencia instalada e falha
# a importar o fastapi.
PATH="/opt/venv/bin:$PATH"
export PATH

# Porta: o launcher nativo le DARK_STUDIO_PORT (predefinida 8013, ver
# .env.example) e o default historico do CORS em backend/app.py tambem e 8013.
# O container ouve na porta que o compose publica, por isso as duas tem de mudar
# em conjunto.
PORT="${DARK_STUDIO_PORT:-8013}"

# --- storage/ --------------------------------------------------------------
# A app cria uploads/ e outputs/ no arranque (ver STORAGE_DIR/UPLOAD_DIR/
# OUTPUT_DIR em backend/services/pipeline.py), mas nao media/, thumbnails/ nem
# music/. Criar os cinco aqui garante que o volume nomeado tem a estrutura
# completa antes do primeiro request.
#
# A imagem ja cria estes diretorios com owner darkstudio, e um volume nomeado
# vazio montado aqui herda essa propriedade. Se o volume ja existir e tiver sido
# criado com outro owner, o mkdir falha e o container aborta aqui - com uma
# mensagem clara, em vez de falhar a meio de um render. A correcao e
# `docker compose down -v` (ou `docker volume rm <nome>`), que recria o volume
# com o owner da imagem.
STORAGE_DIR="$APP_DIR/storage"
for sub in media outputs uploads thumbnails music; do
    mkdir -p "$STORAGE_DIR/$sub"
done

# --- preflight -------------------------------------------------------------
# check_env.py valida interpretador, pacotes, ffmpeg/ffprobe/node/npx, permissao
# de escrita em storage/ e presenca de chaves. Corre-lo aqui, e nao no primeiro
# render, e a diferenca entre "o container morreu ao arrancar" e "o video falha
# 40 segundos depois, com o utilizador a ver".
#
# `--fix` cria as pastas em falta (idempotente). Sem `--strict`: chaves de API em
# falta e pacotes opcionais como o faster-whisper sao AVISO, nao FALHA - o
# servidor arranca e o log diz o que falta. O que e FALHA (pacote obrigatorio em
# falta, Python antigo, storage/ ilegivel) sai com codigo 1 e, com `set -e`,
# aborta o arranque.
echo "  [dark-studio] preflight: a verificar o ambiente..."
if [ -f "$APP_DIR/scripts/check_env.py" ]; then
    "$APP_DIR/scripts/check_env.py" --fix
else
    # O verificador e um ficheiro versionado e pode nao estar nesta copia do
    # projeto. Nao o substituiros: o bloco abaixo faz a verificacao minima
    # equivalente - os quatro binarios de que o render depende - e aborta com a
    # mesma firmeza. O que fica por verificar sao os pacotes Python, e isso e
    # responsabilidade do requirements.txt, verificado em build no Dockerfile.
    echo "  [dark-studio] AVISO: scripts/check_env.py ausente nesta copia."
    echo "  [dark-studio] A correr apenas a verificacao minima de binarios."
    for bin in ffmpeg ffprobe node npx; do
        if ! command -v "$bin" >/dev/null 2>&1; then
            echo "  [dark-studio] ERRO: '$bin' nao esta no PATH. A imagem esta quebrada." >&2
            exit 1
        fi
    done
    echo "  [dark-studio] ffmpeg, ffprobe, node e npx: OK."
fi

# --- arranque do servidor --------------------------------------------------
# 0.0.0.0 e obrigatorio: a app nao conhece a rede do Docker, e um uvicorn ligado
# a 127.0.0.1 so responde dentro do proprio container, o que torna a porta
# publicada inerte. O start.sh usa 127.0.0.1 porque corre no desktop do
# utilizador - aqui seria um erro.
echo "  [dark-studio] a arrancar uvicorn em 0.0.0.0:${PORT} (UI: /app/)"
echo "  [dark-studio] Ctrl+C ou 'docker stop' para encerrar."

# Sem --reload: o codigo e copiado para a imagem, nao montado por bind mount, e
# o auto-reload do uvicorn consome memoria e reinicia o processo a cada
# alteracao de ficheiro em storage/ durante um render.
python -m uvicorn backend.app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --no-access-log &
UVICORN_PID=$!

# --- SIGTERM / SIGINT ------------------------------------------------------
# `docker stop` envia SIGTERM ao PID 1. Se o script nao o encaminhar, o container
# so morre quando o timeout de 10 s do Docker estoura em SIGKILL - com o Chrome a
# meio de um frame e o ficheiro de saida truncado.
#
# O handler limita-se a encaminhar o sinal; o `wait` abaixo faz o resto.
shutdown() {
    echo "  [dark-studio] sinal recebido, a encerrar o uvicorn..."
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# `wait` e o que mantem este script vivo enquanto o uvicorn corre, e devolve o
# codigo de saida do uvicorn. Com `set -e` activo um `wait` que devolvesse != 0
# abortaria o script antes de o codigo ser lido, por isso e desactivado aqui
# explicitamente e o resultado e guardado.
set +e
wait "$UVICORN_PID"
RC=$?
set -e

echo "  [dark-studio] uvicorn terminou com codigo ${RC}."
exit "$RC"
