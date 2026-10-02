# Dark Studio

**PT-PT.** O Dark Studio e uma mesa de montagem de video falado que corre inteiramente na sua maquina. Da um audio sai um MP4 vertical com legendas, imagens de stock e cortes pensados para redes sociais: um servidor FastAPI pesquisa stock media cena a cena, monta uma composicao HTML com GSAP e deixa o HyperFrames renderiza-la com Chrome headless. A IA e sempre um modelo **`:free`** do OpenRouter, e o facto de nunca haver um parametro para pedir um modelo pago e uma invariante do projecto, nao um accidento.

**EN.** Dark Studio is a local narrated-video assembly bench. From an audio file it produces a vertical MP4 with subtitles, stock footage and social-media cuts: a FastAPI server searches stock media scene by scene, assembles a GSAP/HTML composition and lets HyperFrames render it with headless Chrome. The AI is always an OpenRouter **`:free`** model, and the fact that no parameter can ever request a paid model is a project invariant, not an accident.

Documentacao: **[Instalacao](docs/INSTALL.md)** · **[Arquitetura](docs/ARCHITECTURE.md)** · **[API](docs/API.md)**

---

## Funcionalidades

Cada linha aponta para o ficheiro e o simbolo que a implementam.

**Conteudo e texto**
- **Transcricao de audio para SRT** — `pipeline.transcribe_audio_file` usa `faster-whisper` (modelo `tiny`, CPU, `int8`) e escreve `storage/uploads/<projeto>.srt`; `POST /api/transcribe` expoe.
- **Extracao de termos de pesquisa** — `pipeline.extract_keywords_from_text` descarta a estrutura do SRT e as stopwords portuguesas, penaliza flexoes vagas (`-mente`, `-ando`, `-acao`) e intercala bigramas com palavras soltas.
- **Destaques automaticos para shorts** — `shorts_pipeline.detect_highlights` pergunta ao modelo pelos momentos mais fortes; sem chave ou sem quota, uma heuristica por comprimento e espalhamento devolve sempre alguma coisa.
- **Legendas karaoke por palavra** — `shorts_pipeline.extract_words_with_timestamps` distribui o tempo de cada segmento pelas palavras, pesando pelo comprimento de cada token.
- **Angulo editorial de um nicho** — `POST /api/strategy` devolve `angle`, `hook`, tres `titles`, `visual_direction` e tres `chapters`, com fallback local declarado.

**Imagem**
- **Stock media Pexels e Pixabay** — `pipeline.fetch_provider_media` pesquisa os dois fornecedores; a Pexels devolve sempre `kind: "image"`, a Pixabay tambem video.
- **Pesquisa de media **por cena** com termos em ingles escritos por IA** — `pipeline.extract_visual_terms_with_ai` pede ao modelo dois termos por cena; sem ele, `pipeline.extract_scene_keywords` extrai da legenda da propria cena. `pipeline.search_media_for_scenes` junta as duas. Cada cena recebe no maximo 4 resultados, ate 2 termos.
- **Rejeicao de imagens com tela branca** — `asset_quality.looks_padded` mede com `ffmpeg signalstats` duas profundidades em cada borda e rejeita o asset, que passa a candidato seguinte. O resultado entra em `render.rejected_assets`.
- **Cache de media por URL** — `pipeline.download_media_asset` guarda em `storage/media/<projeto>/scene_<i>_<hash>.<ext>`, com o hash do URL na chave para que a substituicao por qualidade progredir.

**Video**
- **Render HyperFrames via npx** — `render_engine.render_with_hyperframes` corre `npx --yes hyperframes@0.8.92 render`, que lanca Chrome headless.
- **Composicao HTML com GSAP** — `render_engine.generate_composition_html` emite `tl.fromTo(...)` para cada cena: fade, wipe de entrada, Ken Burns, e desfoque + recuo do lado que sai.
- **Grain e barra de progresso** — `_grain_overlay` e `_progress_bar` usam passos absolutos em tempos absolutos (nao `infinite`), por isso o resultado e identico em qualquer frame.
- **Storyboard com sobreposicao de 0.7 s** — `pipeline.normalize_scene_timings` faz cada cena comecar antes do seu tempo de origem para que o wipe tenha o que revelar; os fins ficam na timeline de origem, para a narracao ficar sincronizada.
- **Assets copiados para dentro da composicao** — `render_engine.stage_project_assets` copia tudo para `storage/outputs/.hf_<nome>/assets/`, porque o HyperFrames recusa recursos locais fora do directorio do projecto.
- **Cortes ao ritmo (beat-sync)** — `viral_pipeline.detect_beats` usa `librosa.beat.beat_track`; `build_beat_synced_storyboard` crava os cortes nos batidos, com a primeira cena limitada a 3 s para servir de gancho.
- **Loop continuo, thumbnail e metadados de plataforma** — `viral_pipeline.make_seamless_loop`, `generate_optimized_thumbnail` e `build_platform_metadata` (hashtags, categoria, tres titulos, sugestoes de som, horarios de publicacao).
- **Formatos** — `vertical` 1080x1920, `square` 1080x1080, `landscape` 1920x1080 (`render_engine.get_dimensions`).
- **Streaming dos MP4** — `GET /api/project/{nome}/video` e `/shorts/video` devolvem `video/mp4` directamente, com `404` (e nao JSON) para um `<video>` poder falhar de forma visivel.

**Confianca e operacao**
- **Contrato `AUTH_*` uniforme** — `auth_contract.AuthError` transporta `error_code`, `message`, `details` e `status_code`; `backend/app.py:39` instala o handler que o devolve tal e qual. Seis codigos: `AUTH_MISSING_KEY`, `AUTH_INVALID_KEY`, `AUTH_RATE_LIMIT`, `AUTH_QUOTA_EXCEEDED`, `AUTH_MODEL_NOT_FREE`, `AUTH_REQUEST_FAILED`.
- **Tratamento do erro no navegador** — `frontend/auth-handler.js` expoe `window.AuthHandler`: reenvia com backoff os codigos retentaveis, redirecciona para `/#settings` os de chave em falta ou rejeitada, e mostra um toast nos restantes.
- **Multi-fornecedor, multi-chave** — `provider_registry.ProviderRegistry` suporta OpenRouter, NVIDIA, OpenCode, Gemini e OpenAI, com tres formas de dar varias chaves (`<PREFIX>_API_KEY`, `<PREFIX>_API_KEY_1..5`, `<PREFIX>_API_KEYS` separado por virgulas), desativacao automatica de uma chave ao fim de cinco erros e memoria de quota lida dos cabecalhos `x-ratelimit-*`.
- **So modelos `:free`** — verificado em tres camadas: `POST /api/settings` recusa com `400` um modelo sem `:free`, `provider_registry._call_openrouter_compat` volta a verificar antes de cada chamada, e nao existe parametro nenhum que permita pedir um modelo pago.
- **WebUI servida pelo backend** — `backend/app.py` monta `frontend/` em `/app/`.
- **Docker** — `Dockerfile` de tres estagios (`browser` com Chrome via Puppeteer, `builder` com o virtualenv, `runtime`), utilizador sem privilegios (uid 10001), volume nomeado para `storage/`, `init: true` para o Chrome nao deixar zombies e um `HEALTHCHECK` com `start-period` de 40 s. **O build falha tal como esta** — ver [Limitacoes](#limitacoes-conhecidas).

---

## Arranque rapido

### Windows (plataforma de referencia)

**1. Pre-requisitos**

    winget install --id Gyan.FFmpeg -e
    # e instale o Node.js LTS de https://nodejs.org/en/download

Feche e reabra a consola para o `PATH` ser relido, e confirme:

    ffmpeg -version
    node --version

**2. Codigo e ambiente virtual**

    git clone <url-do-repositorio> Dark-Studio
    cd Dark-Studio
    python -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt

**3. `librosa` e `numpy` — obrigatorios, ver [Limitacoes](#limitacoes-conhecidas)**

    .venv\Scripts\python -m pip install "librosa>=0.10.1" "numpy>=1.24.0"

**4. Chaves (opcional mas recomendado)**

    copy .env.example .env

Preencha `OPENROUTER_API_KEY` (IA) e pelo menos uma de `PEXELS_API_KEY` / `PIXABAY_API_KEY` (imagens). Sem chaves nenhuma a aplicacao arranca e produz videos com fundos de cor.

**5. Arrancar**

    start.bat

Interface em <http://127.0.0.1:8013/app/>, documentacao interactiva em <http://127.0.0.1:8013/docs>. `Ctrl+C` para parar.

Instalacao detalhada passo a passo, com o que fazer quando cada passo falha: **[docs/INSTALL.md](docs/INSTALL.md)**.

### Linux e macOS

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python -m pip install "librosa>=0.10.1" "numpy>=1.24.0"
    cp .env.example .env
    sudo apt install ffmpeg        # ou: brew install ffmpeg
    bash start.sh

`start.sh` tem shebang bash e usa `&>/dev/null`: corra com `bash`, nao com `sh`.

### Docker

    cp .env.example .env      # preencher pelo menos OPENROUTER_API_KEY
    docker compose up -d --build
    # http://localhost:8013/app/

Com GPU (acelera so o `faster-whisper`):

    docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

**O `docker compose build` falha tal como o repositorio esta.** Sao dois motivos, ambos verificaveis, e nenhum deles e do Docker:

1. O `Dockerfile:333` corre, no fim do build, `python -c "import fastapi, uvicorn, numpy, soundfile, librosa, edge_tts"`. O `requirements.txt` nao declara `numpy`, `soundfile`, `librosa` nem `edge_tts` — o check foi escrito para partir o build, e cumpre o objectivo, mas parte-o a mais.
2. O `Dockerfile:283` faz `COPY scripts/ /app/scripts/` e a pasta `scripts/` **nao tem um unico ficheiro versionado** (`git ls-files scripts` nao devolve nada). Num clone limpo a pasta nem existe.

Instalacao Docker completa, com o que corrigir e o que esperar: **[docs/INSTALL.md#5-docker](docs/INSTALL.md#5-docker)**.

---

## Pre-requisitos

| Componente | Versao | Obrigatorio | O que acontece sem ele |
|------------|--------|-------------|------------------------|
| **Python** | >= 3.10 | sim | O arranque falha. |
| **ffmpeg** e **ffprobe** | build recente | para resultados correctos | **O servidor arranca e o video sai errado, nao o servidor.** Sem `ffprobe`, `pipeline.get_media_duration` devolve `None`: o storyboard nao e enquadrado na duracao real do audio e o `total_duration` passa a ser o maior `end` do storyboard. Sem `ffmpeg`, `asset_quality.looks_padded` devolve sempre `False` e a rejeicao de assets com tela branca deixa de funcionar. Nenhum dos dois produz erro visivel. |
| **Node.js** + **npx** | LTS 20+ | para renderizar | **O servidor arranca e o render falha a meio.** `render_with_hyperframes` apanha o `FileNotFoundError` e devolve `render.status == "error"` com `npx not found. Please install Node.js and npm.` O primeiro render descarrega tambem ~170 MB de Chrome. |
| **Espaco** | ~1 GB | sim | Chrome e caches do npm nao cabem; o render falha a meio de escrever. |
| **RAM** | 4 GB (8 GB confortavel) | sim | Chrome a 1080x1920 + ffmpeg + numpy/librosa acaba com o processo a meio de um render. |
| **Rede** | saida HTTPS | recomendado | Sem IA, o `/api/strategy` cai no fallback local (200, com `source: "fallback-local"`). Sem stock media, o video sai com fundos de cor. |

O `requirements.txt` fixa `fastapi==0.111.0`, `uvicorn[standard]==0.30.1` e `pydantic==2.7.4`, e relaxa `httpx`, `python-multipart`, `python-dotenv`, `faster-whisper`, `pytest` e `pytest-asyncio`.

---

## Configuracao

Todas as chaves vivem em `.env` na raiz do projecto (copie `.env.example`). A aplicacao tambem as escreve sozinha em `POST /api/settings`, que e a via recomendada a partir da WebUI.

| Variavel | Para que serve | Obrigatoria |
|----------|----------------|-------------|
| `OPENROUTER_API_KEY` | Chave do modelo de IA (guiao de estrategia, termos visuais por cena, destaques). | nao — sem ela a IA cai no fallback local |
| `OPENROUTER_MODEL` | Id do modelo. **Tem de terminar em `:free`**; o resto e recusado com `400`. Predefinido: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`. | nao |
| `PEXELS_API_KEY` | Stock media de <https://www.pexels.com/api/>. | nao — sem chave (ou `PIXABAY_API_KEY`) o video sai com fundos de cor |
| `PIXABAY_API_KEY` | Stock media de <https://pixabay.com/api/docs/>. | nao |
| `GEMINI_API_KEY` | Fornecedor alternativo de IA. A sua presenca **muda o caminho do gateway** para multi-fornecedor. | nao |
| `OPENAI_API_KEY` | Idem. | nao |
| `YOUTUBE_API_KEY` | Apenas aparece como `enabled` em `/api/providers`. | nao |
| `DARK_STUDIO_PORT` | Porta do uvicorn **dentro do container** (`docker/entrypoint.sh`). No arranque nativo a porta 8013 esta fixa nos dois scripts e a variavel e ignorada. | nao (predefinido 8013) |
| `DARK_STUDIO_ALLOWED_ORIGINS` | Passada pelo `docker-compose.yml`, mas `backend/app.py:46` **nao a le**: as origens CORS estao fixas no codigo. | nao |
| `PYTHONUTF8`, `PYTHONIOENCODING` | Exportadas pelo `entrypoint.sh`. Nao pelo `start.bat` desta versao, que usa so `chcp 65001`. | recomendado |
| `PUPPETEER_CACHE_DIR` | Onde o Puppeteer procura o Chrome. Definido pelo `Dockerfile`. | nao |

`AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION` sao passadas pelo `docker-compose.yml`, mas **nenhum modulo deste checkout as le** — pertencem ao modulo de TTS, que aqui nao existe.

Mais detalhe em [docs/API.md#portas-e-origem](docs/API.md#portas-e-origem).

---

## Correr os testes

    .venv\Scripts\python.exe -m pytest

259 testes em 9 ficheiros, **sem rede** por omissao. Os testes que fariam rede ou chamariam um fornecedor estao mockados no `backend.app` ou no `httpx.post`.

Marcadores (declarados em `pytest.ini`):

| Marcador | Onde | Como excluir |
|----------|------|--------------|
| `slow` | `tests/test_e2e.py`, classe `TestBuildVideo` — faz um `/api/build-video` completo contra um servidor real | `-m "not slow"` |
| `live` | `tests/test_live_providers.py` (todo o ficheiro) — chamadas reais | `-m "not live"` |

    .venv\Scripts\python.exe -m pytest -m "not slow and not live"     # o mais rapido
    .venv\Scripts\python.exe -m pytest -m live                         # so as chamadas reais

**`pytest.ini` nao desmarca nada.** Um `pytest` sem argumentos inclui os testes `live`; cada um salta sozinho se a chave correspondente nao estiver no `.env`. **Com chaves configuradas, um `pytest` sem `-m "not live"` consome quota real** — cada teste `live` faz uma chamada ao modelo `:free` do OpenRouter (10 a 60 s cada, e o tier gratuito bloqueia de vez em quando) e ate quatro chamadas ao Pexels/Pixabay. E a raza de `test_live_providers.py` ser separado do resto: o contrato do gateway ja esta coberto offline em `tests/test_ai_gateway.py`.

Dois avisos:

- `tests/test_e2e.py` importa `requests`, que **nao** esta no `requirements.txt`.
- Qualquer teste que importe `backend.app` precisa de `librosa` instalado.

| Ficheiro | Cobre |
|---------|-------|
| `tests/test_ai_gateway.py` | `extract_json_payload`, modo JSON, retries transitorios, contrato de `/api/strategy` offline, quota diaria, estado dos fornecedores. |
| `tests/test_asset_quality.py` | Dimensoes, deteccao de brilho uniforme, `looks_padded`, `describe`. |
| `tests/test_auth.py` | Chave em falta, chave invalida, validacao de modelo, fallback de estrategia, contrato `AUTH_*`. |
| `tests/test_e2e.py` | Sonda a um uvicorn real: `/health`, `/api/providers`, `/api/strategy`, servir `/app/`, contrato `AUTH_*`, e um `/api/build-video` completo marcado `slow`. |
| `tests/test_live_providers.py` | **Marcado `live` no ficheiro inteiro.** Chamadas reais ao modelo e ao Pexels/Pixabay; cada um salta se a chave nao estiver no `.env`. |
| `tests/test_media_render.py` | A composicao contem media, tempos sequenciais, staging de assets, rejeicao de padding, cache por URL, escape de HTML, keywords, ciclo do SRT. |
| `tests/test_shorts.py` | Descoberta de audio, destaques, tempos por palavra, storyboard, HTML karaoke, contrato de `generate_shorts`. |
| `tests/test_viral.py` | Deteccao de batidas, loop, thumbnail, metadados, integracao do pipeline. |
| `tests/test_visual_terms.py` | Extracao de termos, termos por IA, `search_media_for_scenes`, relatorio de `term_source` em `/api/build-video`. |

---

## Resolucao de problemas no Windows

O objectivo do projecto e funcionar no Windows sem erros. Resumo; o guia completo, com comandos e explicacoes, esta em [docs/INSTALL.md#7-resolucao-de-problemas-no-windows](docs/INSTALL.md#7-resolucao-de-problemas-no-windows).

**Primeiro, o diagnostico.** este checkout **nao tem `scripts/check_env.py`** (o verificador de ambiente a que o brief se refere). Em vez dele:

    .venv\Scripts\python.exe -c "import fastapi, uvicorn, numpy, librosa; print('deps OK')"
    ffmpeg -version
    node --version
    curl http://127.0.0.1:8013/health

**`ModuleNotFoundError: No module named 'librosa'`.** O `requirements.txt` nao o declara, mas `backend/app.py` importa `viral_pipeline` no topo, que importa `librosa` no topo. `pip install "librosa>=0.10.1" "numpy>=1.24.0"`.

**O `start.bat` nao encontra o `.venv`.** Verifica `.venv\Scripts\python.exe`. Se existe mas nao executa (o antivirus quarantineou), apague e refaca: `rmdir /s /q .venv`, `python -m venv .venv`, `pip install -r requirements.txt`.

**Python da Microsoft Store.** O stub de 0 KB faz `python -m venv .venv` criar um `.venv` sem conteudo. Instale o Python de python.org com **"Add python.exe to PATH"**, ou desactive os executores `python.exe`/`python3.exe` em Definicoes > Aplicacoes > Executores de aplicacao.

**Porta ocupada.** O `start.bat` desta versao tem a porta fixa: mude `--port 8013` e `set DARK_STUDIO_PORT=9000` no ficheiro, e a origem CORS em `backend/app.py:46`. Descubra o dono com `netstat -ano -p tcp | findstr ":8013"` e `taskkill /PID <pid> /F`.

**`[WinError 10013]` numa porta livre.** O Windows reserva faixas de portas (Hyper-V, WSL2, Xbox Game Bar) e a `bind()` recebe `10013` em vez de `10048` mesmo sem processo nenhum. Veja a faixa com `netsh interface ipv4 show excludedportrange protocol=tcp`. Se a 8013 estiver la dentro, mude de porta.

**Caminhos com acentos, espacos ou unidade de rede.** Clique duas vezes no `start.bat` (o Windows abre-o ja na pasta do projecto — este `start.bat` nao faz `cd /d "%~dp0"`). Nao use `C:\Program Files\`. Um projecto em `\\servidor\...` deve ser copiado para o disco local: o `pip` e a escrita em `storage/` falham em unidades de rede.

**Antivirus em quarentena.** Se o Windows Defender puser o `.venv` em quarentena, ve **Seguranca do Windows > Proteccao contra virus e Ameacas > Historico**, apague a pasta e refaca. Nao marque a pasta do projecto como confiavel se estiver num sitio partilhado.

**Erros de codificacao na consola.** `start.bat` faz `chcp 65001` mas nao exporta `PYTHONUTF8`. Se aparecer um `UnicodeEncodeError`, arranque com `set PYTHONUTF8=1` e `set PYTHONIOENCODING=utf-8`.

**A pagina abre mas nada acontece.** A WebUI chama a API com caminhos **relativos** (`const apiBase = ''`, `frontend/index.html:32`), pelo que so funciona servida pelo proprio backend em `/app/`. Um erro de CORS significa origem errada — so `http://127.0.0.1:8013/app/` e `http://localhost:8013/app/` sao aceites. Em particular, **nao use `Abrir Dark Video Studio.bat`**: ele serve a pasta `frontend/` com `python -m http.server 8080`, e cada `fetch('/api/...')` vai parar ao servidor de ficheiros estaticos, que responde `404` em HTML — o `response.json()` lanca depois. Alem disso nao mostra janela do servidor e depende de `Get-NetTCPConnection` (modulo NetTCPIP).

**Legendas erradas sem erro nenhum.** Compare o texto que `POST /api/transcribe` devolve com o audio. Sem `faster-whisper`, `transcribe_audio_file` devolve cinco frases fixas em portugues — e um `200` normal.

---

## Arquitetura

    Navegador  http://127.0.0.1:8013/app/
        |  fetch()  (CORS: apenas 127.0.0.1:8013 e localhost:8013)
        v
    backend/app.py         FastAPI + uvicorn, bind 127.0.0.1
        |
        +-- POST /api/transcribe --> faster-whisper --> <projeto>.srt
        |
        +-- POST /api/build-video
        |     +-- parse_srt_to_segments            SRT        -> segmentos
        |     +-- extract_keywords_from_text       segmentos  -> termos
        |     +-- build_storyboard_from_segments   segmentos  -> cenas (+ overlap 0.7 s)
        |     +-- search_media_for_scenes          cenas      -> media, termos por IA
        |     |     +-- extract_visual_terms_with_ai   (IA, ingles)
        |     |     +-- extract_scene_keywords         (heuristica)
        |     |     +-- search_media_for_keywords -> Pexels / Pixabay
        |     +-- build_edit_plan_from_segments
        |     +-- render_video_hyperframes
        |           +-- create_project_dir         storage/outputs/.hf_<nome>/
        |           +-- stage_project_assets       copy -> assets/, rejeita canvas branco
        |           +-- generate_composition_html  HTML + CSS + GSAP
        |           +-- render_with_hyperframes    npx --yes hyperframes@0.8.92 render
        |           +-- cleanup_render_dirs
        |     -> storage/outputs/<projeto>.mp4
        |
        +-- POST /api/shorts      destaques + karaoke -> <projeto>_shorts.mp4 (sem audio)
        +-- POST /api/build-viral  beats (librosa) + loop + thumbnail -> <projeto>_viral.mp4
        +-- POST /api/strategy    call_free_model -> OpenRouter :free, ou fallback local

Mapa de modulos, layout de dados, decisoes de desenho e o porque de cada uma: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

Decisoes que vale a pena conhecer, em uma linha cada:
- **HyperFrames em vez de FFmpeg direto** — as transicoes e o Ken Burns sao animacoes de timeline, que em GSAP sao `tl.fromTo(...)` com tempos absolutos (seek-safe e reproduzivel) e em `filter_complex` seriam `xfade` + `gblur` afinados a mao por cena.
- **Assets copiados para dentro da composicao** — o HyperFrames recusa recursos locais fora do directorio do projecto; um caminho absoluto para `storage/media` simplesmente nao carrega.
- **Sobreposicao de 0.7 s entre cenas** — para o wipe ter algo que revelar; os fins ficam na timeline de origem para a narracao nao se desincronizar.
- **So modelos `:free`** — invariante em tres camadas, sem nenhuma forma de pedir um modelo pago.

---

## Limitacoes conhecidas

**O `requirements.txt` esta incompleto e o servidor nao arranca sem `librosa`.** `viral_pipeline.py` importa `librosa` e `numpy` no topo e `backend/app.py` importa esse modulo no topo. Nenhum dos dois esta no `requirements.txt`; o `numpy` chega por via do `faster-whisper`, o `librosa` nao chega por lado nenhum. Uma instalacao limpa falha com `ModuleNotFoundError` no arranque.

**Faltam modulos ao projecto.** Nao existem nesta copia `backend/services/tts.py` (sintese de voz), `script_gen.py` (geracao de guiao por IA), `style.py` (temas e estilos de legenda) nem `music.py` (biblioteca e upload de musica). Por isso **nao ha** `/api/script`, `/api/tts`, `/api/voices`, `/api/languages`, `/api/tts/status`, `/api/presets` nem `/api/music/*`. A unica via de entrada de conteudo e um audio que o utilizador carregue. A tipografia das legendas esta fixa em `render_engine._TRANSITIONS_CSS`, com quatro variantes (bottom, center, hook, karaoke) em vez de oito temas.

**`scripts/check_env.py` nao existe.** Nao ha preflight antes do arranque, e o `Dockerfile` faz `COPY scripts/ /app/scripts/` sobre uma pasta sem ficheiros versionados: **o build do Docker falha** tal como esta.

**So modelos `:free`, e o tier tem quota diaria.** A politica e intencional, mas o preco e que `OPENROUTER` tem um limite diario de pedidos para modelos gratuitos. Quando esgota, a resposta e `AUTH_QUOTA_EXCEEDED` com `scope: "daily"`; o mesmo codigo sai quando os creditos da conta acabam. O `/api/strategy` degrada para um fallback local (200, com `source: "fallback-local"`), mas **os termos visuais por cena e os destaques dos shorts perdem qualidade** sem modelo: passam a heuristica por palavras.

**O stock media depende de chaves de terceiros.** Sem `PEXELS_API_KEY` e sem `PIXABAY_API_KEY`, `search_media_for_scenes` devolve `{}` e cada cena fica com a cor de fundo. O video renderiza — sem fotografia. Uma chave rejeitada ou uma API em erro **nao** viram uma lista vazia: dao `AUTH_INVALID_KEY` / `AUTH_RATE_LIMIT`, para nao parecer que a pesquisa "nao tem resultados".

**Um GPU nao acelera o render.** O HyperFrames compoe frames em Chrome headless (que corre com `--disable-gpu`) e o encoder de saida e o ffmpeg. O `docker-compose.gpu.yml` existe e acelera **apenas** o `faster-whisper`. Para renders mais rapidos: mais CPU e mais RAM.

**A transcricao degrada em silencio.** `transcribe_audio_file` engole qualquer excecao do `faster-whisper` e devolve cinco frases fixas em portugues com 3 segundos cada. Um SRT errado nao produz erro em lado nenhum: `/api/transcribe` responde `200` e `/api/build-video` rende um video confiavelmente errado. E o defeito mais facil de nao detectar.

**A porta e as origens CORS estao fixas no codigo.** `start.bat` e `start.sh` nao leem `DARK_STUDIO_PORT` nem procuram uma porta livre acima dela — a 8013 esta fixa nos dois. `backend/app.py:46` permite apenas `http://127.0.0.1:8013` e `http://localhost:8013`, sem `allow_origin_regex` e sem ler `DARK_STUDIO_ALLOWED_ORIGINS`. Para mudar a porta ou a origem e preciso editar o codigo.

**O cache de media nunca e invalidado.** `_MEDIA_CACHE` vive em memoria do processo e a memoria de quota do OpenRouter tambem. Reiniciar o servidor limpa ambos; durante uma sessao longa, uma fotografia que o fornecedor deixou de servir continua a ser servida.

**Os testes `live` correm por omissao.** `pytest.ini` declara os marcadores mas nao os desmarca: um `pytest` sem `-m "not live"` faz chamadas reais e gasta quota. Ver [Correr os testes](#correr-os-testes).

**A WebUI e um unico ficheiro.** `frontend/index.html` tem o CSS e o JavaScript todos inline (112 linhas, minificados) e carrega apenas `auth-handler.js` como modulo separado. Nao ha `styles.css`, nem `js/main.js`, nem `js/views/`: nao ha build step, nem separacao de concerns, nem versioning por componente. Para uma aplicacao que grows, e a primeira coisa a refazer. O lado bom e que a WebUI nao tem um unico ficheiro de 404 por carregar.

---

## Estrutura

    backend/
      app.py                        FastAPI: 14 endpoints, CORS, montagem de /app
      services/
        pipeline.py                 SRT, keywords, storyboard, pesquisa de media, gateway
        render_engine.py            composicao HTML/GSAP + HyperFrames via npx
        shorts_pipeline.py          destaques, karaoke, corte vertical
        viral_pipeline.py           beats, loop, thumbnail, metadados
        asset_quality.py            deteccao de imagem com tela branca (ffmpeg)
        provider_registry.py        multi-fornecedor, multi-chave, retries, quota
        auth_contract.py            AuthError e o contrato AUTH_*
    frontend/
      index.html                  WebUI completa, CSS e JS inline
      auth-handler.js             contrato AUTH_* no navegador
    storage/
      uploads/  outputs/  media/  thumbnails/
    tests/                          259 testes, 9 ficheiros
    scripts/                        (vazio nesta copia)
    Dockerfile  docker-compose.yml  docker-compose.gpu.yml  docker/entrypoint.sh
    pytest.ini  requirements.txt  .env.example
    start.bat  start.sh  "Abrir Dark Video Studio.bat"
