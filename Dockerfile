# syntax=docker/dockerfile:1
# =============================================================================
#  Dark Studio - imagem de producao
#
#  O render NAO e "so" Python. backend/services/render_engine.py corre
#      npx --yes hyperframes@0.8.92 render
#  que arranca Puppeteer -> Chrome headless, e asset_quality.py / pipeline.py /
#  render_engine.py correm `ffmpeg` e `ffprobe` em subprocess com uma lista de
#  argumentos. Uma imagem python:slim arranca o servidor e falha a meio da
#  geracao de video, que e o pior sitio possivel para descobrir que faltam
#  binarios. Por isso o build verifica os quatro binarios no fim.
#
#  Debian slim, nunca Alpine: o Chrome, o ffmpeg e a pilha de fontes sao muito
#  mais fiaveis em glibc, e o projeto e Windows-first (glibc parity importa).
# =============================================================================

# Imagem base dos dois estagios Python. docker-compose.gpu.yml substitui este
# valor por uma imagem CUDA (ver esse ficheiro para o que o GPU compra aqui).
ARG BASE_IMAGE=python:3.12-slim-bookworm

# Pinos explicitos: uma tag movel torna o build irreproduzivel e empurra uma
# regressao para producao.
ARG NODE_VERSION=22.14.0
ARG PUPPETEER_CHROME_VERSION=stable


# -----------------------------------------------------------------------------
# Estagio 1 - browser: descarrega o Chrome que o Puppeteer usa.
#
# Num estagio separado porque (a) o node:22-bookworm-slim e muito mais pequeno
# que o python com ffmpeg, e (b) o download do Chrome (~170 MB) fica numa camada
# so, que o Docker cacheia independentemente do codigo da aplicacao.
# O Chrome e copiado para /opt/puppeteer no estagio final; em runtime o Puppeteer
# encontra-o pela variavel PUPPETEER_CACHE_DIR.
# -----------------------------------------------------------------------------
FROM node:22-bookworm-slim AS browser

ARG PUPPETEER_CHROME_VERSION

ENV PUPPETEER_CACHE_DIR=/opt/puppeteer \
    PUPPETEER_SKIP_DOWNLOAD=1

# unzip e obrigatorio, e o que esta a falhar sem ele: o @puppeteer/browsers
# descarrega um .zip e, em vez de extrair em Node, delega num arquivador do
# sistema. Sem o `unzip` instalado falha com "Extraction failed: no zip
# archiver is available". ca-certificates e preciso para o HTTPS do download.
#
# Usamos "chrome@stable" em vez de uma versao exata porque e a serie que o
# Puppeteer atual valida; a serie fica fixada pela imagem node:22 deste estagio,
# o que torna o resultado reproduzivel para o proposito.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    npx --yes @puppeteer/browsers install \
        "chrome@${PUPPETEER_CHROME_VERSION}" \
        --path /opt/puppeteer

# O Chrome traz um binario chrome-sandbox separado. Dar-lhe o bit setuid e owner
# root e o que permite ao sandbox do Linux funcionar. O caminho inclui a versao,
# por isso e resolvido com um glob em vez de hardcoded.
#
# O chrome-sandbox nao e garantido: as builds "Chrome for Testing" mais recentes
# nao o distribuem. A ausencia nao e um erro de build, e a raza de o wrapper no
# estagio final levar --no-sandbox. Tratar a ausencia como aviso e o que mantem
# o build a passar em builds com e sem sandbox.
RUN set -eux; \
    CHROME_BIN="$(find /opt/puppeteer -type f -name chrome -perm -u+x | head -n 1)"; \
    CHROME_DIR="$(dirname "$CHROME_BIN")"; \
    echo "chrome dir: $CHROME_DIR"; \
    chown -R root:root "$CHROME_DIR"; \
    if [ -f "$CHROME_DIR/chrome-sandbox" ]; then \
        chmod 4755 "$CHROME_DIR/chrome-sandbox"; \
        echo "chrome-sandbox: setuid aplicado (o sandbox nativo do Chrome fica disponivel)."; \
    else \
        echo "AVISO: chrome-sandbox ausente nesta build; o wrapper usara --no-sandbox."; \
    fi


# -----------------------------------------------------------------------------
# Estagio 2 - builder: dependencias Python num virtualenv.
#
# Num virtualenv para que o estagio final copie /opt/venv e nao obtenha uma copia
# de shadow do Python do sistema (pip, setuptools, etc.).
# -----------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# O python3 ja vem na imagem base por omissao (python:3.12-slim-bookworm), mas a
# base CUDA que o docker-compose.gpu.yml usa e Ubuntu e nao tem Python nenhum.
# Instalar so quando falta mantem o build normal identico e torna o override GPU
# funcional sem um Dockerfile separado.
RUN set -eux; \
    if ! command -v python3 >/dev/null 2>&1; then \
        apt-get update; \
        apt-get install -y --no-install-recommends python3 python3-venv python3-pip; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    python3 -m venv /opt/venv

WORKDIR /build

# Copiado sozinho primeiro: a camada de dependencias so e invalidada quando o
# requirements.txt muda, nao a cada alteracao ao codigo da aplicacao.
COPY requirements.txt /build/requirements.txt

# Sem --no-deps: a lista e a declaracao exacta de dependencias do projeto, e o
# servidor importa varios destes modulos no topo dos seus ficheiros. Inclui
# tambem os pacotes de teste (pytest, pytest-asyncio) porque estao declarados
# aqui; quem quiser uma imagem mais pequena pode filtrar, ao custo de desviar do
# requirements.txt.
RUN /opt/venv/bin/python -m pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/python -m pip install -r /build/requirements.txt


# -----------------------------------------------------------------------------
# Estagio 3 - runtime: so o virtualenv e os binarios de runtime.
# -----------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

ARG NODE_VERSION

# Lista de pacotes, por ordem de porque sao necessarios:
#
#   ffmpeg              traz ffmpeg E ffprobe, os dois binarios que
#                       asset_quality.py (ffmpeg, ffprobe), pipeline.py
#                       (ffprobe) e render_engine.py (ffmpeg, ffprobe) lancam em
#                       subprocess. Sem eles o servidor arranca e a geracao de
#                       video falha a meio.
#   ca-certificates     obrigatorio, nao opcional: httpx (OpenRouter, Pexels,
#                       Pixabay), edge-tts e os downloads de media sao HTTPS.
#                       Sem a cadeia de confianca falham com SSL verify error.
#   curl                descarrega o tarball do Node abaixo. Fica na imagem
#                       porque sem ele o build quebra em qualquer rede com proxy.
#   xz-utils            o tarball do Node e .tar.xz; tar precisa de xz para o
#                       descomprimir.
#   libgomp1            runtime OpenMP que o onnxruntime (faster-whisper) liga.
#   libsndfile1         audio nativo que o soundfile (librosa) carrega em
#                       runtime; sem ela o `import librosa` quebra.
#   libasound2          ALSA: o Chrome precisa de abrir um dispositivo de audio
#                       mesmo em headless, e aborta sem ele.
#   libnss3, libnspr4   Network Security Services: autenticacao TLS do Chrome.
#   libatk1.0-0,
#   libatk-bridge2.0-0  Accessibility Toolkit: GTK depende dele.
#   libatspi2.0-0       (via apt) a camada de atspi que olibatk liga.
#   libcups2            impressao: exigido pelo GTK no momento de ligar o
#                       toolkit, mesmo sem impressora.
#   libdbus-1-3         barramento de mensagens; o Chrome fala por dbus.
#   libdrm2, libgbm1    backend de buffer management para a GPU - libgbm e o
#                       "no usable sandbox" mais comum em contentores.
#   libglib2.0-0        base de dados GObject, de que tudo acima depende.
#   libpango-1.0-0      layout de texto; sem ele nao ha TEXTO no video.
#   libx11-6, libxext6  X11 base; o Chrome liga sempre a X11.
#   libxcomposite1,
#   libxdamage1,
#   libxfixes3,
#   libxrandr2,
#   libxshmfence1       extensoes X11 que o Chrome usa para gesto das janelas.
#   libxkbcommon0       teclado.
#   libxss1            aceleracao de som por X11 (requested na tarefa; mantido
#                       porque e inofensivo e aparece em varias listas oficiais).
#   libgtk-3-0          GTK 3: o Chrome carrega-o mesmo em headless e aborta sem
#                       ele.
#   fontconfig          o motor que resolve "Segoe UI", Tahoma, Arial, sans-serif
#                       numa familia concreta. Sem ele nenhuma fonte e encontrada.
#   fonts-liberation    Arial/Tahoma metricamente equivalentes - a composicao
#                       HyperFrames pede 'Segoe UI', Tahoma, Arial, sans-serif.
#   fonts-dejavu-core   fallback generico do sans-serif.
#   fonts-noto-color-emoji  emoji em texto.
#
# NOTA SOBRE FONTES: os quatro ultimos NAO SAO OPCIONAIS. A composicao queima
# texto estilizado nos frames (legendas, headlines, o gancho em caixa alta).
# Sem fontes, o Chrome renderiza com o que o fontconfig resolver, as legendas
# desaparecem SILENCIOSAMENTE, e o video sai com audio e imagem certa e zero
# texto - sem qualquer erro em nenhum log. E o unico item desta imagem cuja
# ausencia produz resultado errado em vez de falha visivel.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        xz-utils \
        libgomp1 \
        libsndfile1 \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libgtk-3-0 \
        fontconfig \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-color-emoji \
    ; \
    rm -rf /var/lib/apt/lists/*

# Node LTS do tarball oficial em vez do repositorio da distro: o Debian 12 traz
# Node 18 (EOL) e o hyperframes/Puppeteer atuais assumem Node 20+. A arquitetura
# vem do dpkg para o build funcionar tambem em arm64.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "arquitetura nao suportada" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm -f /tmp/node.tar.xz; \
    node --version; \
    npm --version; \
    npx --version

COPY --from=browser /opt/puppeteer /opt/puppeteer

ENV PUPPETEER_CACHE_DIR=/opt/puppeteer \
    PUPPETEER_SKIP_DOWNLOAD=1

# O symlink e recriado AQUI, e nao no estagio browser: so /opt/puppeteer e
# copiado entre estagios, o /usr/local/bin do estagio browser e descartado.
#
# O wrapper acrescenta os argumentos que faltam ao Chrome neste ambiente:
# --no-sandbox           o Chrome recusa o seu proprio sandbox dentro de um
#                        contentor sem CAP_SYS_ADMIN e aborta com "No usable
#                        sandbox!". O chrome-sandbox setuid do estagio browser e
#                        a primeira escolha; este wrapper e o plano B para hosts
#                        sem user namespaces.
# --disable-dev-shm-usage o /dev/shm do Docker tem 64 MB por omissao e o Chrome
#                        rebenta a escrever um frame de 1080x1920. O compose
#                        aumenta shm_size; esta opcao cobre quem corre o
#                        contentor a mao.
# --disable-gpu          nao ha GPU de video util num servidor de render - o
#                        encode e feito pelo ffmpeg, nao pelo Chrome.
#
# O `--version` no fim e uma sanity check de verdade: carrega o linkador e todas
# as bibliotecas partilhadas. Um Chrome que exista em disco mas falhe por
# falta de uma .so so se descobre no primeiro render.
RUN set -eux; \
    CHROME_BIN="$(find /opt/puppeteer -type f -name chrome -perm -u+x | head -n 1)"; \
    ln -sf "$CHROME_BIN" /usr/local/bin/chrome-real; \
    printf '%s\n' \
        '#!/bin/sh' \
        'exec /usr/local/bin/chrome-real --no-sandbox --disable-dev-shm-usage --disable-gpu "$@"' \
        > /usr/local/bin/chrome-headless; \
    chmod 0755 /usr/local/bin/chrome-headless; \
    /usr/local/bin/chrome-headless --version

COPY --from=builder /opt/venv /opt/venv

# Utilizador sem privilegios. UID/GID explicitos (nao os automaticos do
# useradd) porque um volume nomeado criado a partir deste path herda o owner da
# imagem - com um UID dinamico, os ficheiros ficariam inacessiveis.
RUN set -eux; \
    groupadd --gid 10001 darkstudio; \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin darkstudio

WORKDIR /app

# requirements.txt e scripts/ entram antes do codigo: sao o que o cache tem de
# invalidar quando mudam, e nao o que o utilizador muda todos os dias.
COPY requirements.txt /app/requirements.txt
COPY scripts/ /app/scripts/
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY docker/entrypoint.sh /app/docker/entrypoint.sh

RUN chmod 0755 /app/docker/entrypoint.sh

# storage/ vive num volume nomeado, mas e criado na imagem com o owner certo
# para que um volume vazio montado em /app/storage herde essa propriedade.
# A lista de subdiretorios segue a que o codigo assume: media, outputs e
# uploads vem de STORAGE_DIR/UPLOAD_DIR/OUTPUT_DIR em pipeline.py, thumbnails e
# music sao usados pelos servicos de media e de banda sonora.
RUN set -eux; \
    mkdir -p /app/storage/media /app/storage/outputs /app/storage/uploads \
             /app/storage/thumbnails /app/storage/music; \
    chown -R darkstudio:darkstudio /app/storage; \
    chmod -R u+rwX /app/storage

# PATH com o virtualenv: o entrypoint tambem o exporta, mas o HEALTHCHECK e
# executado pelo Docker sem passar pelo entrypoint e sem estas variaveis, e o
# `python` que ele invoca tem de ser o do virtualenv.
#
# O codigo e pt-PT e escreve nao-ASCII para a consola e para ficheiros. Numa
# imagem minima o locale e POSIX/C e um acento num log rebenta o processo;
# PYTHONUTF8/PYTHONIOENCODING sao a rede de seguranca que o start.sh tambem
# define no Windows, onde o mesmo problema existe em cp850.
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/app \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DARK_STUDIO_PORT=8013 \
    HOME=/home/darkstudio

# Sanidade de build: os quatro binarios de que o render depende tem de estar no
# PATH, AGORA. Sem esta linha um pacote mal instalado so se descobre na
# primeira geracao de video em producao, com o utilizador a ver.
RUN set -eux; \
    for bin in ffmpeg ffprobe node npx; do command -v "$bin"; done; \
    ffmpeg -version | head -n 1; \
    ffprobe -version | head -n 1; \
    node --version; \
    npx --version; \
    # Estes sao os modulos que o grafo de import de backend/app.py carrega no
    # topo dos modulos, e por isso tem de estar presente ANTES de qualquer
    # request. A verificacao e estrita, sem fallback: um pacote obrigatorio em
    # falta no requirements.txt tem de fazer o BUILD falhar, que e o unico
    # momento em que ainda ha alguem para corrigir. Um `||` aqui apenas
    # moveria a falha para o primeiro render em producao.
    /opt/venv/bin/python -c "import fastapi, uvicorn, numpy, soundfile, librosa, edge_tts"; \
    # Cache do npm criada aqui, com o owner certo, para que o primeiro
    # `npx --yes hyperframes@...` em runtime nao tente escrever em /root.
    mkdir -p /home/darkstudio/.npm /home/darkstudio/.cache; \
    chown -R darkstudio:darkstudio /home/darkstudio

USER darkstudio

EXPOSE 8013

# O /health e estatico (devolve {"status":"ok"} sem tocar em disco nem em rede).
# Usa python e nao curl para nao installar curl so para isto. start-period
# generoso: o primeiro arranque importa numpy/librosa, que demoram varios
# segundos, e um healthcheck agressivo reinicia o container em loop no warm-up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8013/health', timeout=4).status == 200 else 1)"]

# Nao ha ENTRYPOINT: o compose usa `init: true`, que poe o tini do proprio
# Docker como PID 1 e recolhe os processos zombie que o Chrome deixa. Quem
# correr o container sem compose deve usar `docker run --init`.
CMD ["/app/docker/entrypoint.sh"]
