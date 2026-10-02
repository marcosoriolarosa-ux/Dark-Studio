# Instalacao — Dark Studio

Guia passo a passo, **Windows primeiro**. Cada passo indica o comando exato, o que e esperado ver e o que fazer quando falha.

Para a superficie HTTP ver [API.md](API.md). Para o desenho ver [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 0. Aviso importante antes de comecar

O brief do projecto menciona sintese de voz, geracao de guiao por IA, temas de legenda e musica de fundo. **Nada disso existe nesta copia do repositorio:**

| Falta | Consequencia |
|-------|--------------|
| `backend/services/tts.py` | Nao ha voz sintetizada: a narracao tem de vir de um audio que o utilizador carregue. Nao ha `/api/tts`, `/api/voices` nem `/api/languages`. |
| `backend/services/script_gen.py` | Nao ha geracao de guiao a partir de um tema. Nao ha `/api/script`. |
| `backend/services/style.py` | Nao ha temas nem estilos de legenda; a tipografia esta fixa no CSS do render. Nao ha `/api/presets`. |
| `backend/services/music.py` | Nao ha musica de fundo nem `/api/music/*`. |

O caminho Docker **existe** (`Dockerfile`, `docker/entrypoint.sh`, `docker-compose.yml`, `docker-compose.gpu.yml`, `.dockerignore`), mas **nao constrói tal como o repositorio esta** — ver [passo 0.1](#01-o-build-docker-falha-e-porquê) e a [secao 5](#5-docker).

`scripts/check_env.py` tambem nao existe: a pasta `scripts/` esta vazia (só `__pycache__`, e nenhum ficheiro versionado — `git ls-files scripts` nao devolve nada). O `entrypoint.sh:71` trata isso sem quebrar: avisa e faz a verificacao minima dos quatro binarios.

Isto tem uma consequencia concreta e **bloqueante**, tanto no arranque nativo como no build Docker: o `requirements.txt` esta incompleto.

    pip install -r requirements.txt     # completo no papel
    uvicorn backend.app:app             # ModuleNotFoundError: No module named 'librosa'

Motivo: `backend/services/viral_pipeline.py` faz `import librosa` e `import numpy` no topo do modulo, e `backend/app.py` importa esse modulo no topo. **`librosa` e `numpy` tem de ser instalados a mao**:

    pip install "librosa>=0.10.1" "numpy>=1.24.0"

### 0.1 O build Docker falha, e porquê

O `Dockerfile:333` termina o build com uma verificacao de sanidade deliberadamente estrita:

    /opt/venv/bin/python -c "import fastapi, uvicorn, numpy, soundfile, librosa, edge_tts"

O comentario acima dela explica a intencao: *"um pacote obrigatorio em falta no requirements.txt tem de fazer o BUILD falhar, que e o unico momento em que ainda ha alguem para corrigir"*. A intencao e boa; o problema e que o `requirements.txt` **nao declara nenhum desses quatro pacotes** (`numpy`, `soundfile`, `librosa`, `edge_tts`). O `numpy` chega por via do `faster-whisper`, os outros tres nao chegam por lado nenhum. O build para ali, no ultimo passo, depois de ter feito download do Chrome e construido o virtualenv.

Alem disso o `Dockerfile:283` faz `COPY scripts/ /app/scripts/` sobre uma pasta **sem um unico ficheiro versionado**. Num clone limpo a pasta nem existe e o build falha mais cedo, com um erro de `COPY` pouco explicito.

Dois problemas, entao, e nenhum e do Docker. Corrigir o `requirements.txt` resolve os dois de uma vez, e e a unica correccao que pertence a este projecto:

    pip install "librosa>=0.10.1" "numpy>=1.24.0" soundfile

Requer atencao a `edge_tts`: nao ha modulo de TTS neste repositorio (`backend/services/tts.py` nao existe), logo o `edge-tts` e uma dependencia orfa. A sanidade de build foi escrita para um projecto que tinha TTS.

---

## 1. Pre-requisitos

| Componente | Versao | Para que serve | O que acontece sem ele |
|------------|--------|----------------|------------------------|
| **Python** | 3.10 ou superior | Servidor e servicos | O arranque falha. |
| **ffmpeg** + **ffprobe** | qualquer build recente | Medir duracao de audio e video (`pipeline.get_media_duration`), detetar imagens com tela branca (`asset_quality`) | **O servidor arranca e o video sai errado, nao o servidor.** Sem `ffprobe`, a duracao do audio devolve `None`, o storyboard nao e enquadrado na duracao real e o `total_duration` passa a ser o maior `end` do storyboard. Sem `ffmpeg`, `looks_padded` devolve sempre `False` e a rejeicao de assets com canvas branco deixa de funcionar. Nenhum dos dois produz erro visivel. |
| **Node.js** + **npx** | LTS 20 ou superior | O HyperFrames corre `npx --yes hyperframes@0.8.92 render`, que lanca Chrome headless | **O servidor arranca e o render falha a meio.** `render_with_hyperframes` apanha o `FileNotFoundError` e devolve um payload com `status: "error"` e a mensagem `npx not found. Please install Node.js and npm.` O primeiro render tambem descarrega ~170 MB de Chrome. |
| **Espaco em disco** | ~1 GB | Chrome descarregado pelo Puppeteer + caches do npm | O render falha ao meio de escrever. |
| **RAM** | 4 GB (8 GB confortavel) | Chrome a 1080x1920 + ffmpeg + numpy/librosa | O processo morre a meio de um render. |
| **Rede** | saida HTTPS | OpenRouter, Pexels, Pixabay, download do Chrome | Sem IA o `/api/strategy` cai no fallback local (200, com `source: "fallback-local"`). Sem stock media o video sai com fundos de cor, sem imagens. |

### Instalar o ffmpeg (Windows)

    winget install --id Gyan.FFmpeg -e

Ou, com Chocolatey:

    choco install ffmpeg

Feche e reabra a consola depois de instalar, para o `PATH` ser relido. Verifique:

    ffmpeg -version
    ffprobe -version

### Instalar o Node.js (Windows)

Instale o **Node.js LTS** de <https://nodejs.org/en/download>. Deixe marcada a opcao de instalar as ferramentas de native. Verifique:

    node --version
    npx --version

O `node` tem de estar **no `PATH` da mesma consola** que vai correr o servidor. Se installou o Node e `node --version` nao responder, o `PATH` da sessao ainda esta velho: feche e reabra a consola.

---

## 2. Windows — instalacao manual, passo a passo

### Passo 1 — Obter o codigo

    git clone <url-do-repositorio> Dark-Studio
    cd Dark-Studio

Ou descompacte o ZIP em **`C:\Dark-Studio`**. Evite `C:\Program Files\...` e unidades de rede (ver [Resolucao de problemas](#7-resolucao-de-problemas-no-windows)).

### Passo 2 — Criar o ambiente virtual

**PowerShell:**

    py -3 -m venv .venv

**cmd.exe:**

    python -m venv .venv

Esperado: aparece `.venv\` com `Scripts\python.exe`. Se `py` nao for reconhecido, use `python`; se `python` nao for reconhecido, instale o Python de <https://www.python.org/downloads/> marcando **"Add python.exe to PATH"**.

> Nao use o Python da Microsoft Store. O stub que a Store instala (`python.exe` de 0 KB que abre a loja em vez de correr) faz o `venv` criar-se sem conteudo e produz um `.venv\Scripts\python.exe` que nao arranca. Instale o Python de python.org, ou desactive os executores em **Definicoes > Aplicacoes > Executores de aplicacao**.

### Passo 3 — Instalar as dependencias

**PowerShell:**

    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

**cmd.exe:**

    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt

Esperado: `Successfully installed fastapi-0.111.0 uvicorn-0.30.1 ...`.

> Use sempre `.venv\Scripts\python.exe -m pip`, **nunca** o `pip` solto — o `pip` solto pode ser de outro Python e instala no sitio errado.

### Passo 4 — Instalar o `librosa` e o `numpy` (obrigatorio, ver [passo 0](#0-aviso-importante-antes-de-comecar))

    .venv\Scripts\python.exe -m pip install "librosa>=0.10.1" "numpy>=1.24.0"

Esperado: `Successfully installed librosa-0.10.x numpy-1.26.x ...`. O primeiro `import librosa` demora varios segundos (compila o numba); e normal.

### Passo 5 — Configurar as chaves

    copy .env.example .env

Abra o `.env` num editor e preencha o que quiser usar. **Nunca comite um `.env` com chaves reais** — esta no `.gitignore`, e a aplicacao tambem o escreve sozinha em `POST /api/settings`.

| Variavel | Obrigatoria | Para que serve |
|----------|--------------|----------------|
| `OPENROUTER_API_KEY` | nao | Modelo de IA. Sem ela, `/api/strategy` e os termos visuais por cena caem no fallback local. **Sem esta chave o projecto ainda funciona**: e so menos inteligente. |
| `OPENROUTER_MODEL` | nao | Tem de terminar em `:free`. Predefinido: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`. |
| `PEXELS_API_KEY` | nao | Stock media de <https://www.pexels.com/api/>. |
| `PIXABAY_API_KEY` | nao | Stock media de <https://pixabay.com/api/docs/>. |
| `GEMINI_API_KEY`, `OPENAI_API_KEY`, `YOUTUBE_API_KEY` | nao | Mostram-se como `enabled` em `/api/providers`. As duas primeiras activam o caminho multi-fornecedor do gateway de IA. |

Sem nenhuma chave, a aplicacao arranca, aceita um audio, transcreve e rende um video com fundos de cor e sem imagem de stock. Com pelo menos `PEXELS_API_KEY` ou `PIXABAY_API_KEY`, o video tem imagens.

### Passo 6 — Arrancar

    start.bat

Esperado:

    ============================================
      DARK VIDEO STUDIO - Iniciar Servidor
    ============================================

    [1/3] A ativar ambiente virtual...
    [2/3] A iniciar servidor uvicorn na porta 8013...
    > Servidor: http://127.0.0.1:8013
    > Interface: http://127.0.0.1:8013/app/
    > Documentacao: http://127.0.0.1:8013/docs
    [3/3] A abrir navegador...

O navegador abre sozinho em `http://127.0.0.1:8013/app/`. Se nao abrir, va a esse endereco a mao. Para terminar: `Ctrl+C` na janela do servidor.

> O `start.bat` abre o navegador **antes** do uvicorn estar pronto. Se a pagina carregar em branco na primeira vez, e isso: recarregue (F5) dois segundos depois.

### Passo 7 — Verificar

Noutra consola:

    curl http://127.0.0.1:8013/health

Esperado: `{"status":"ok","message":"Dark Video Studio MVP running"}`

E o estado dos fornecedores:

    curl http://127.0.0.1:8013/api/providers

Se `pexels.enabled` for `false` e voce configurou a chave, o `.env` nao foi lido — reinicie o servidor.

---

## 3. Windows — os dois atalhos de arranque

O repositorio traz dois ficheiros `.bat`. **Nao sao equivalentes.**

| Ficheiro | O que faz | Recomendado |
|----------|-----------|-------------|
| `start.bat` | Ativa o `.venv`, arranca o uvicorn na porta 8013 na janela do servidor, abre o navegador. | **Sim.** |
| `Abrir Dark Video Studio.bat` | Verifica o `.venv`, arranca o uvicorn na 8013 **minimizado** e um `python -m http.server 8080` separado para servir a pasta `frontend/`, abrindo `http://127.0.0.1:8080/`. | Nao. Ver abaixo. |

Porque nao o segundo:

1. Serve a interface na **porta 8080** com um servidor de ficheiros estaticos. A WebUI chama a API com caminhos **relativos** (`const apiBase = ''`, `frontend/index.html:32`), pensados para a mesma origem: servida pelo backend em `/app/` funciona, servida pelo `http.server` cada `fetch('/api/transcribe')` vai parar ao servidor de ficheiros, que responde `404` em HTML, e o `response.json()` lanca a seguir. A pagina aparece com aspecto correcto e **nao faz nada**.
2. Nao mostra nenhuma janela do servidor, portanto nao ha log nem `Ctrl+C` — fecha-se pelo Gerenciador de Tarefas.
3. Depende de `Get-NetTCPConnection`, que precisa do modulo NetTCPIP carregado. Se o modulo nao estiver disponivel, o script sai em silencio sem arrancar nada.

Se precisar mesmo de uma interface sem janela do servidor visivel, arranque o `start.bat` normalmente.

---

## 4. Linux e macOS

    git clone <url-do-repositorio> Dark-Studio
    cd Dark-Studio

    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python -m pip install "librosa>=0.10.1" "numpy>=1.24.0"

    cp .env.example .env      # preencher OPENROUTER_API_KEY e/ou PEXELS_API_KEY

    # ffmpeg
    sudo apt install ffmpeg                 # Debian / Ubuntu
    brew install ffmpeg                     # macOS

    # Node LTS pelo gestor de pacotes da distro, ou de https://nodejs.org/en/download

    bash start.sh

A interface fica em `http://127.0.0.1:8013/app/`.

> `start.sh` tem shebang `#!/usr/bin/env bash` e usa `&>/dev/null`, portanto **tem de ser corrido com `bash`** (ou `chmod +x start.sh` e depois `./start.sh`). `sh start.sh` falha em varios pontos.

> O `.venv/` que vem nesta copia do repositorio e um virtualenv **do Windows** (`Lib/`, `Scripts/`, `Include/`). Apague-o antes de seguir estes passos:
>
>     rm -rf .venv

---

## 5. Docker

O caminho Docker existe e e serio: `Dockerfile` de tres estagios, `docker/entrypoint.sh` em `sh` (dash, sem bashisms), `docker-compose.yml`, `docker-compose.gpu.yml` e `.dockerignore`, com comentarios a explicar cada decisao. O que **falha hoje e o build** — ver [0.1](#01-o-build-docker-falha-e-porquê).

### 5.1 O que o build faz

| Estagio | O que faz |
|---------|-----------|
| `browser` | `node:22-bookworm-slim` + Puppeteer, descarrega o Chrome para `/opt/puppeteer` com `PUPPETEER_SKIP_DOWNLOAD=1`. |
| `builder` | `python:3.12-slim-bookworm` + venv em `/opt/venv`, `pip install -r requirements.txt`. |
| `runtime` | `python:3.12-slim-bookworm` + Node 22.14.0 + Chrome do estagio `browser` + o venv copiado. Utilizador `darkstudio` (uid 10001), sem privilegios. |

O runtime instala `ffmpeg` (que traz `ffprobe`), `ca-certificates`, `libgomp1` (OpenMP do onnxruntime), `libsndfile1` (o `soundfile` que o `librosa` carrega), `libasound2` (o Chrome precisa de um dispositivo de audio mesmo em headless) e as bibliotecas NSS/GTK do Chrome. Cada uma tem o porque comentado no `Dockerfile`.

### 5.2 Corrigir o build

Antes de conseguir construir, o `requirements.txt` tem de declarar o que o `Dockerfile:333` importa:

    # em requirements.txt, antes de construir
    librosa>=0.10.1
    numpy>=1.24.0
    soundfile>=0.12.1

E a pasta `scripts/` tem de ter pelo menos um ficheiro versionado, porque o `Dockerfile:283` a copia. Com `scripts/check_env.py` ausente, o `entrypoint.sh:71` ja trata o caso: avisa e corre so a verificacao dos quatro binarios. Para um build limpo, `git add scripts/check_env.py` (ou um `.gitkeep` na pasta) e o `COPY` passa.

O `edge_tts` da linha 333 e o ponto que exige uma decisao: nao ha modulo de TTS neste repositorio, logo ou se remove da verificacao de sanidade, ou se adiciona o `edge-tts` ao `requirements.txt` para um modulo que nao existe.

### 5.3 Correr

    cp .env.example .env      # preencher pelo menos OPENROUTER_API_KEY
    docker compose up -d --build
    # http://localhost:8013/app/

    docker compose logs -f dark-studio      # preflight + arranque do uvicorn
    docker compose down                    # parar (o volume sobrevive)
    docker compose down -v                 # parar e apagar os videos gerados

Com GPU — acelera **apenas** o `faster-whisper`. O render corre em Chrome headless com `--disable-gpu`, e o encoder de saida e o ffmpeg:

    docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

O `docker-compose.gpu.yml` tem de ser usado como sobreposicao, nunca sozinho.

### 5.4 O que o compose configura, e o que nao faz

| Chave | Efeito real |
|-------|-------------|
| `ports: "8013:8013"` | Publica a porta. **E a variavel `DARK_STUDIO_PORT` tem de bater certo com ela**: o `entrypoint.sh:40` le essa variavel para ligar o uvicorn, e o compose nao a usa para construir o `ports:`. Mudar uma sem a outra deixa o container a ouvir num sitio e publicado noutro. |
| `DARK_STUDIO_PORT` | **Lida** pelo `entrypoint.sh:40` (predefinido 8013). O `start.bat` e o `start.sh` **nao** a leem: no arranque nativo a porta esta fixa em 8013. |
| `DARK_STUDIO_ALLOWED_ORIGINS` | **Nao e lida por lado nenhum.** O `backend/app.py:46` tem as origens CORS escritas no codigo. O valor que o compose passa nao tem efeito nenhum, e o comentario do compose que explica a necessidade de acesso remoto esta a descrever um mecanismo que nao existe. Para acesso remoto, e preciso editar `backend/app.py`. |
| `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` | Passadas ao container, **nao lidas por nenhum modulo** (pertencem ao modulo de TTS, que nao existe). |
| `volumes: dark-studio-storage` | O unico estado que o utilizador nao pode perder. A pasta e criada na imagem com o owner `darkstudio`; um volume vazio herda esse owner. **Se o volume ja tiver sido criado por outra versao da imagem, o container aborta no arranque** — o `mkdir` do `entrypoint.sh:56` falha com `set -e`. Resolucao: `docker compose down -v`. |
| `init: true` | `tini` como PID 1, para o Chrome deixar de acumular processos zombie. Com `docker run` directo, usar `--init`. |
| `shm_size: "2gb"` | O `/dev/shm` do Docker tem 64 MB por omissao e o Chrome rebenta com *"session deleted because of page crash"* num frame de 1080x1920. |
| `mem_limit` | **Descomentado de proposito.** Chrome a 1080x1920 + ffmpeg + numpy/librosa somam 2-3 GB num render. As sugestoes estao no proprio ficheiro (6 GB para maquinas de 8 GB, 12 GB para 16 GB). |
| `restart: unless-stopped` | O container volta a levantar depois de um reboot. |
| `env_file: .env` | `required: false` (Compose >= 2.24), para `docker compose up` funcionar numa maquina limpa. |
| `logging` | `json-file` com rotacao de 20 MB x 5 ficheiros. |

### 5.5 Diagnostico do container

    docker compose ps                 # o container esta "healthy"?
    docker compose logs dark-studio   # o preflight e o arranque
    docker compose exec dark-studio sh -c "ffmpeg -version; node --version; python -c 'import librosa'"

Sintomas:

- **O build para em `import fastapi, uvicorn, numpy, soundfile, librosa, edge_tts`** — ver [5.2](#52-corrigir-o-build).
- **O container reinicia em loop no arranque** — o healthcheck tem `start-period 40s` propositadamente generoso, porque o primeiro arranque importa numpy e librosa (varios segundos). Se mesmo assim reiniciar, o log mostra o motivo: quase sempre e o `mkdir` do `storage/` num volume com owner errado, e resolve-se com `docker compose down -v`.
- **`ERRO: 'ffmpeg' nao esta no PATH. A imagem esta quebrada.`** — a mensagem do `entrypoint.sh:83`, no caminho sem `check_env.py`. So acontece se a imagem foi construida com a cache de camadas errada; refaca com `--no-cache`.
- **A porta publicada responde mas a UI nao chama a API** — o backend so aceita `http://127.0.0.1:8013` e `http://localhost:8013`. Aceder por `http://192.168.1.50:8013` da um erro de CORS, e ajustar `DARK_STUDIO_ALLOWED_ORIGINS` **nao resolve**, porque ninguem a le.

---

## 6. Desenvolvimento a partir do codigo

Auto-reload:

    .venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8013 --reload

Correr a suite de testes:

    .venv\Scripts\python.exe -m pytest
    .venv\Scripts\python.exe -m pytest -m "not slow and not live"   # o mais rapido

259 testes em 10 ficheiros, sem rede por omissao. Detalhes em [README.md](README.md#correr-os-testes).

Linters nao ha: o projecto nao traz `ruff`, `flake8` nem `mypy` configurados. A convencao do codigo e pt-PT em comentarios e mensagens, nomes de simbolo em ingles, e um comentario por bloco que explica o *porque* e nao o *o que*.

---

## 7. Resolucao de problemas no Windows

### O primeiro diagnostico

Este checkout **nao tem `scripts/check_env.py`**. Em vez dele, faca estas quatro verificacoes:

    .venv\Scripts\python.exe -c "import fastapi, uvicorn, numpy, librosa; print('deps OK')"
    ffmpeg -version
    node --version
    curl http://127.0.0.1:8013/health

O primeiro comando e o que mais falha: e ele que apanha o `librosa` em falta do `requirements.txt`.

### `ModuleNotFoundError: No module named 'librosa'`

O `requirements.txt` nao declara `librosa`, mas `backend/app.py` importa `viral_pipeline` no topo e esse modulo importa `librosa` no topo. Corrija com:

    .venv\Scripts\python.exe -m pip install "librosa>=0.10.1" "numpy>=1.24.0"

### `npx not found. Please install Node.js and npm.`

O `POST /api/build-video` devolve `200` com `render.status == "error"`. Falta o Node no `PATH` da consola que corre o servidor. Instale o Node LTS, **feche e reabra a consola** (o `PATH` so e relido em novas sessoes) e arranque de novo.

### `[ERROR] Ambiente virtual nao encontrado.`

O `start.bat` verifica se existe `.venv\Scripts\python.exe`. Crie-o e instale as dependencias (passos 2 e 3). Se a pasta existe mas nao executa — ver antivirus abaixo — apague e refaca:

    rmdir /s /q .venv
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt

### Python da Microsoft Store

O stub de 0 KB faz `python -m venv .venv` criar um `.venv` sem conteudo. Instale o Python de python.org com **"Add python.exe to PATH"**, ou desative os executores `python.exe` / `python3.exe` em **Definicoes > Aplicacoes > Executores de aplicacao**.

### A porta 8013 ja esta em uso

`[WinError 10048]` ao arrancar o uvicorn. Descubra o dono:

    netstat -ano -p tcp | findstr ":8013"

O ultimo campo e o PID. Encerre-o:

    taskkill /PID <pid> /F

Para mudar a porta, **esta versao do `start.bat` tem-na fixa em dois sitios** (a mensagem e o comando `uvicorn`) e as origens CORS estao fixas em `backend/app.py:46`. Nao existe `DARK_STUDIO_PORT` que faca isto por si. Edite os tres sitios, ou arranque o uvicorn a mao:

    .venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 9000

### `[WinError 10013]` numa porta que ninguem esta a usar

O Windows reserva faixas de portas para o Hyper-V, o WSL2 e o Xbox Game Bar, e uma porta reservada da `bind()` mesmo sem processo nenhum. O uvicorn recebe `10013` em vez de `10048`.

    netsh interface ipv4 show excludedportrange protocol=tcp

Se a 8013 aparecer na faixa reservada, a unica solucao e mudar a porta (acima).

### Caminhos com acentos, espacos ou caracteres especiais

O `start.bat` desta versao **nao** faz `cd /d "%~dp0"`, ao contrario do `Abrir Dark Video Studio.bat`, que o faz. Se correr `uvicorn backend.app:app` a partir de outra pasta, o modulo `backend` nao esta no `PYTHONPATH` e o arranque falha com `ModuleNotFoundError: No module named 'backend'`.

Por isso:

1. **Clique duas vezes no `start.bat`** — o Windows abre-o ja com a pasta do projecto como pasta de trabalho.
2. Se precisar de o correr de uma consola, mude primeiro para a pasta.
3. Se o projecto estiver numa **unidade de rede** (`\\servidor\...`), copie-o para o disco local: o `pip` e a escrita em `storage/` falham em unidades de rede.
4. Evite uma pasta dentro de `C:\Program Files\` (escrita negada em `storage/`).

Os caminhos que o codigo abre (`storage/`, `.env`) resolvem a partir de `BASE_DIR = Path(__file__).resolve().parents[2]` (`backend/services/pipeline.py:34`), pelo que acentos e espacos no nome do projecto nao os corrompem. O que quebra e o arranque a partir da pasta errada.

### O antivirus pôs o `.venv` em quarentena

Sintoma tipico: o `start.bat` diz que o ambiente virtual nao foi encontrado, ou `.venv\Scripts\python.exe` existe mas da erro imediato ao executar. O Windows Defender e algumas suites de seguranca colocam em quarentena os executaveis dentro de pastas `.venv` de projectos recem-clonarados.

1. **Seguranca do Windows > Proteccao contra virus e Ameacas > Historico de Proteccao** e veja o que foi posto em quarentena.
2. Apague a pasta e refaca a partir do zero — e o caminho mais limpo:
3. Se nao puder desativar o antivirus (politica da empresa), ponha a pasta do projecto numa excepcao.
4. Nao marque a pasta do projecto inteira como confiavel se ela estiver num sitio partilhado ou sincronizado.

### A pagina carrega mas nada acontece

Abra a consola do navegador (F12). Ha dois modos de falha, e a mensagem na consola diz qual e:

**`Unexpected token '<'` ou um `404` na resposta.** A interface esta a ser servida por outra coisa que o backend. A WebUI usa caminhos relativos (`const apiBase = ''`, `frontend/index.html:32`), portanto **tem de ser servida pelo proprio backend**, em `/app/`. Em particular:

- Se usou `Abrir Dark Video Studio.bat` (porta 8080, `python -m http.server`), mude para `http://127.0.0.1:8013/app/`.
- Se abriu o ficheiro com duplo clique (`file:///.../frontend/index.html`), abra pelo backend: nenhum fetch relativo funciona.

**`blocked by CORS policy`.** A origem esta errada. As unicas validas sao `http://127.0.0.1:8013/app/` e `http://localhost:8013/app/`. Se acedeu por `http://[::1]:8013` ou pelo nome da maquina, va a `127.0.0.1`.

### O video sai sem imagens / com fundos de cor

Esperado quando nao ha chave de stock media. `GET /api/providers` mostra `pexels.enabled` e `pixabay.enabled`. Sem nenhuma das duas, `search_media_for_scenes` devolve `{}` e cada cena fica com a cor de fundo — o video renderiza, so sem fotografia. Nao e um bug.

### O video sai com as legendas erradas

Verifique se `faster-whisper` esta instalado:

    .venv\Scripts\python.exe -c "import faster_whisper; print('whisper OK')"

Se nao estiver, `transcribe_audio_file` nao da erro nenhum: devolve cinco frases fixas em portugues com 3 segundos cada. O `/api/transcribe` responde `200` e o SRT fica errado. Compare sempre o texto devolvido com o audio que carregou.

### O primeiro render demora muito

O primeiro `npx --yes hyperframes@0.8.92 render` descarrega o HyperFrames e o Chrome (~170 MB). Os seguintes sao rapidos enquanto a cache do npm estiver intacta. Apagar `%LOCALAPPDATA%\npm-cache` obriga a descarregar de novo.

### `faster-whisper` falha ao descarregar o modelo

O primeiro arranque da transcricao descarrega o modelo Whisper. Sem rede, ou com a cache corrompida, `transcribe_audio_file` engole a excecao e devolve as cinco frases de exemplo (ver acima). Forcar a descarga antes:

    .venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

### Acentos na consola do Windows

`PYTHONUTF8` e `PYTHONIOENCODING` sao o que evita um `UnicodeEncodeError` numa consola cp850. O `start.bat` desta versao define `chcp 65001` mas **nao** exporta as duas variaveis. Se aparecerem erros de codificacao:

    set PYTHONUTF8=1
    set PYTHONIOENCODING=utf-8
    .venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8013

### O `.venv` versionado estraga tudo

A pasta `.venv/` na raiz e um virtualenv do Windows versionado por engano (`Lib/`, `Scripts/`, `Include/`). Nao e usada por ninguem e confunde o diagnostico. Apague-a se nao for a sua:

    rmdir /s /q .venv
