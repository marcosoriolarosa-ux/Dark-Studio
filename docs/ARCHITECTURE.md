# Arquitetura — Dark Studio

Como um tema chega a um MP4, o que cada modulo faz, porque o projecto foi escrito desta forma e onde esta mais fraco.

---

## 1. O pipeline, ponta a ponta

O caminho principal e o seguinte. Cada bloco corresponde a uma etapa real do codigo.

    audio (ou tema, via /api/strategy)
        |
        v
    [1] POST /api/transcribe          backend/app.py:84
        faster-whisper -> segmentos {index, start, end, text}
        |
        v
    [2] build_srt_from_segments       backend/services/pipeline.py:97
        storage/uploads/<projeto>.srt
        |
        v
    [3] POST /api/build-video         backend/app.py:108
        |
        +-- parse_srt_to_segments           pipeline.py:413   SRT -> segmentos
        +-- extract_keywords_from_text      pipeline.py:176   termos globais de pesquisa
        +-- build_storyboard_from_segments  pipeline.py:365   segmentos -> cenas
        +-- search_media_for_scenes         pipeline.py:549   media POR CENA
        |     +-- extract_visual_terms_with_ai   pipeline.py:483  (IA) ou
        |     +-- extract_scene_keywords         pipeline.py:227  (heuristica)
        |     +-- search_media_for_keywords      pipeline.py:447  -> Pexels / Pixabay
        +-- build_edit_plan_from_segments  pipeline.py:393   plano de edicao
        |
        v
    [4] render_video_hyperframes      backend/services/render_engine.py:584
        +-- fit_storyboard_to_duration  pipeline.py:719   enquadra o storyboard no audio
        +-- create_project_dir          render_engine.py:488  storage/outputs/.hf_<nome>/
        +-- stage_project_assets        render_engine.py:262  copia media para assets/
        |     +-- download_media_asset       pipeline.py:668
        |     +-- looks_padded               asset_quality.py:70  (rejeita canvas branco)
        +-- generate_composition_html   render_engine.py:357  composicao GSAP + CSS
        +-- render_with_hyperframes     render_engine.py:544  npx hyperframes render
        +-- cleanup_render_dirs         render_engine.py:525
        |
        v
    storage/outputs/<projeto>.mp4
        |
        v
    [5] GET /api/project/{nome}/video  -> FileResponse video/mp4

Duas derivacoes partem do mesmo SRT:

    [A] POST /api/shorts
        detect_highlights                 shorts_pipeline.py:48   (IA, ou heuristica)
        extract_words_with_timestamps     shorts_pipeline.py:189  karaoke por palavra
        build_highlight_storyboard        shorts_pipeline.py:233  rebase=True: cenas em t=0
        render_video_hyperframes(output_stem="<nome>_shorts", audio_path=None)
        -> storage/outputs/<nome>_shorts.mp4   (sem audio: as palavras estao nas legendas)

    [B] POST /api/build-viral
        detect_beats                      viral_pipeline.py:40    librosa.beat.beat_track
        build_beat_synced_storyboard      viral_pipeline.py:80    cortes presos ao ritmo
        make_seamless_loop                viral_pipeline.py:144
        generate_optimized_thumbnail      viral_pipeline.py:170   HTML em storage/thumbnails/
        build_platform_metadata           viral_pipeline.py:217   hashtags, categoria, titulos
        render_video_hyperframes(output_stem="<nome>_viral")
        -> storage/outputs/<nome>_viral.mp4 + <nome>_viral_meta.json

E um caminho lateral, sem video:

    POST /api/strategy  ->  pipeline.call_free_model  ->  provider_registry  ->  OpenRouter (:free)
                             com fallback local declarado em caso de falha

---

## 2. Mapa de modulos

| Ficheiro | Responsabilidade, numa frase |
|----------|------------------------------|
| `backend/app.py` | A superficie HTTP: 14 endpoints, CORS, saneamento de nomes, montagem da WebUI em `/app`. |
| `backend/services/pipeline.py` | O nucleo de dados: SRT, keywords, storyboard, plano de edicao, pesquisa de media e o re-export do gateway de IA. |
| `backend/services/render_engine.py` | Gera a composicao HTML/CSS/GSAP e corre o HyperFrames via `npx`. |
| `backend/services/asset_quality.py` | Mede com ffmpeg se uma imagem de stock esta embrulhada numa tela branca uniforme. |
| `backend/services/shorts_pipeline.py` | Escolhe destaques, distribui tempos por palavra e monta o corte vertical. |
| `backend/services/viral_pipeline.py` | Batidas, cortes ao ritmo, loop, thumbnail e metadados de plataforma. |
| `backend/services/provider_registry.py` | Chama a IA: multi-fornecedor, multi-chave, retries, memoria de quota. |
| `backend/services/auth_contract.py` | O unico lugar onde nasce um `AuthError` e onde vive o contrato `AUTH_*`. |
| `frontend/index.html` | A WebUI (passo a passo + painel de definicoes). |
| `frontend/auth-handler.js` | Traduz `AUTH_*` em accoes de UI: reenvio, redireccao para definicoes, toasts. |
| `scripts/` | **Vazia nesta copia.** O brief menciona um `scripts/check_env.py` que aqui nao existe. |
| `Dockerfile` | Tres estagios: `browser` (Chrome via Puppeteer), `builder` (venv), `runtime` (utilizador sem privilegios, uid 10001). |
| `docker/entrypoint.sh` | Arranque em `sh` (dash, sem bashisms): `cd /app`, variaveis de locale, preflight, `uvicorn` em `0.0.0.0:$DARK_STUDIO_PORT` e reencaminhamento de `SIGTERM`. |
| `docker-compose.yml` | Volume nomeado para `storage/`, `init: true`, `shm_size: 2gb`, healthcheck, chaves de API todas com `${VAR:-}`. |
| `tests/` | 259 testes em 9 ficheiros, sem rede por omissao. |

### Onde cada responsabilidade NAO esta (nesta copia)

O brief do projecto menciona TTS, geracao de guiao por IA, temas de legendas e musica de fundo. **Nenhum desses modulos existe nesta copia do repositorio.** O caminho Docker existe; o que falta sao os modulos de media que o brief promete:

| Modulo esperado | Estado | Consequencia |
|-----------------|--------|--------------|
| `backend/services/tts.py` | ausente | Nao ha `/api/tts`, `/api/voices`, `/api/languages` nem `/api/tts/status`. A narracao tem de vir de um ficheiro de audio carregado pelo utilizador. |
| `backend/services/script_gen.py` | ausente | Nao ha `/api/script`. O texto de origem e sempre um audio transcrito. |
| `backend/services/style.py` | ausente | Nao ha `/api/presets` nem parametros de estilo. A tipografia das legendas esta fixa em `render_engine._TRANSITIONS_CSS`, com quatro variantes (bottom, center, hook, karaoke) em vez de oito temas. |
| `backend/services/music.py` | ausente | Nao ha `/api/music/*`. A `render_video_hyperframes` atual nao tem `music_track`, `music_volume` nem `duck_voice`, e nao ha qualquer mixagem de musica. |
| `scripts/check_env.py` | ausente | Nao ha verificador de ambiente para correr antes do arranque. Ver [INSTALL.md](INSTALL.md). |
| `frontend/styles.css`, `frontend/js/` | ausentes, **mas nao sao necessarios** | A WebUI e hoje um unico `index.html` com o CSS e o JS todos inline. Nao ha `styles.css`, nem `js/main.js`, nem `js/views/`. |
| `storage/music/` | criado so no container | O `Dockerfile:293` e o `entrypoint.sh:55` criam `storage/music/`, mas nenhum modulo deste checkout escreve la: sobra do modulo de musica que nao existe. |

`librosa` e `numpy` sao importados no topo de `viral_pipeline.py`, e `backend/app.py` importa esse modulo no topo: sao dependencias **obrigatorias de arranque** apesar de nao constarem do `requirements.txt`.

---

## 3. Dados em disco

    storage/
      uploads/         <projeto>.mp3          audio transcrito
                       <projeto>.srt          legendas geradas (a fonte da verdade do video)
      outputs/         <projeto>.mp4          render principal
                       <projeto>_shorts.mp4   corte vertical
                       <projeto>_viral.mp4    corte viral
                       <projeto>.json         resumo do render
                       <projeto>_shorts.json  resumo do short
                       <projeto>_viral_meta.json  batidas + metadados + storyboard
                       .hf_<projeto>/         directorio de trabalho do HyperFrames
                            index.html        a composicao gerada
                            hyperframes.json  manifesto do HyperFrames
                            package.json      scripts `render` e `check`
                            assets/           media e audio copiados para aqui
      media/           <projeto>/scene_<i>_<hash>.jpg   cache de media descarregada
      thumbnails/      <projeto>_thumb.html            documento do thumbnail

Um "projeto" nao e um registo: e um prefixo de nome de ficheiro. `GET /api/projects` devolve os ficheiros, nao os projetos.

O cache de media e chaveado por **hash do URL** alem do indice da cena (`pipeline.py:682`). Keying so pelo indice fazia com que um segundo candidato para a mesma cena devolvesse o ficheiro do primeiro, e a substituicao por qualidade nunca progredia.

---

## 4. Decisoes de desenho, e porque

### HyperFrames em vez de FFmpeg direto

`render_engine.py` nao constroi um `filter_complex` de video. Gera um documento HTML com CSS e GSAP (`generate_composition_html`, linha 357) e deixa o HyperFrames — que corre Chrome headless via `npx` — produzir os frames.

O motivo esta no proprio codigo: as transicoes, o Ken Burns, o desfoque de saida e o overlay de grao sao todos *aninacoes de timeline*. Em FFmpeg seriam `zoompan` + `xfade` + `gblur` com filtros de sobreposicao, afinados a mao por cena. Em GSAP sao `tl.fromTo(...)` com tempos absolutos, e o resultado e seek-safe e reproduzivel frame a frame. O `ffmpeg`/`ffprobe` continuam a ser o apoio para **analise** (duracao, deteccao de padding), que e barato e exato.

Consequencia pratica: o render depende de Node, npx, Chrome e de um download de ~170 MB no primeiro arranque. E o unico gargalo de distribuicao do projecto.

### Pesquisa de media por cena, e nao global

`search_media_for_scenes` (`pipeline.py:549`) pesquisa cada cena com os termos **dessa** cena; `search_media_for_keywords` continua a existir, mas so como pool de recurso quando a pesquisa global devolve alguma coisa e uma cena nao tem nada seu.

O comentario no codigo e explicito: uma lista global de keywords da a todas as cenas a mesma imagem. `extract_visual_terms_with_ai` pede ao modelo dois termos **em ingles** por cena, porque os indices de stock indexam muito melhor ingles do que portugues — "banknotes", nao "papeis coloridos". Sem chave ou sem quota, cai para `extract_scene_keywords`, que extrai da legenda da propria cena. O resultado diz qual dos dois foi usado, em `term_source`.

`build_storyboard_from_segments` alterna `fade`/`slide_left` e `cinematic`/`glow` para que duas cenas consecutivas nunca pareçam iguais.

### Sobrepor cenas para as transicoes

`normalize_scene_timings` (`pipeline.py:319`) faz cada cena comecar `SCENE_OVERLAP = 0.7` segundos antes do seu tempo de origem. O objectivo e explicito: dar a um wipe algo para revelar por cima. Os **fins** ficam na timeline de origem, o que mantem a naracao sincronizada e faz as pausas entre segmentos serem cobertas visualmente pela cena seguinte em vez de silencio morto.

Duas protecoes evitam o caos: um segmento mais curto que `MIN_SCENE_DURATION = 0.8` e esticado ate esse minimo, e duas cenas degeneradas no mesmo instante sao separadas por 0.2 s em vez de empilhadas. As legendas nunca se sobrepoem — quem as recorta e `generate_composition_html`, que sabe onde comeca a vizinha.

### Somente modelos `:free`

E uma invariante em tres camadas:

1. `POST /api/settings` recusa com `400` um `openrouter_model` que nao termine em `:free`.
2. `provider_registry._call_openrouter_compat` volta a verificar antes de cada chamada.
3. Nao existe parametro nenhum, em nenhum endpoint, que permita pedir um modelo pago.

A raza e o objectivo do projecto: uma aplicacao que funciona sem cartao e sem custo por execucao. `provider_registry` suporta varios fornecedores (OpenRouter, NVIDIA, OpenCode, Gemini, OpenAI) e varios metodos de chave (`<PREFIX>_API_KEY`, `<PREFIX>_API_KEY_1..5`, `<PREFIX>_API_KEYS` separado por virgulas), mas so o OpenRouter tem `free_model_filter`, logo e o unico que a politica de gratuito cobre por completo. Uma chave desativada ao fim de cinco erros (`provider_registry._update_key_state`) faz o registo passar a seguinte automaticamente.

### Assets copiados para dentro do directorio da composicao

`stage_project_assets` (`render_engine.py:262`) copia cada ficheiro referenciado para `storage/outputs/.hf_<projeto>/assets/` e a composicao passa a referenciar `assets/<ficheiro>` em vez de um caminho absoluto para `storage/media`.

A raza esta no docstring: o HyperFrames serve a composicao a partir do seu proprio directorio de projecto e **recusa recursos locais fora dele**. Um caminho absoluto para `storage/media` simplesmente nao carrega. A copia resolve tambem a cache entre renders — o directorio e apagado e recriado a cada render, logo nunca sobra um frame antigo.

### Rejeitar imagens com tela branca

`looks_padded` (`asset_quality.py:70`) mede com ffmpeg. Alguns fornecedores devolvem uma JPEG 1880x1246 que e ~45% branco uniforme; `object-fit: cover` reproduz essa tela com fidelity e o video fica com um bloco branco a dar a sensacao de estar partido.

A deteccao faz duas sondas por cada borda — uma rasa (12% do lado menor) e outra funda (35%). A raza e a profundidade que decidem: uma faixa fina e uniforme so prova que a borda e plana; se a regiao aos 35% ainda for plana e clara, o asset esta realmente embrulhado. Um frame uniformemente claro no geral e uma foto de high-key, nao padding. O veredicto e cacheado por hash do conteudo.

A imagem rejeitada nao e descartada: `stage_project_assets` passa ao candidato seguinte do pool, e o payload traz `rejected_assets` com o URL recusado e o motivo.

### Grao e progresso deterministas

`_grain_overlay` e `_progress_bar` animam por **passos absolutos em tempos absolutos**, nunca com `infinite`. O docstring explica: uma animacao CSS `infinite` avanca com o tempo de parede e faria cada render sair ligeiramente diferente; passos limitados e seek-safe dao sempre o mesmo frame no mesmo instante.

---

## 5. Desenho geral

    +-------------------------------------------------------------+
    |  Navegador   http://127.0.0.1:8013/app/                     |
    |  frontend/index.html + auth-handler.js                      |
    +----------------------------+--------------------------------+
                                 | fetch()  (CORS: apenas :8013)
    +----------------------------v--------------------------------+
    |  backend/app.py      FastAPI + uvicorn, bind 127.0.0.1     |
    |    sanitize_name()  ->  storage/uploads/<nome>.{srt,mp3}    |
    +--+--------------+--------------+-------------+--------------+
       |              |              |             |
       v              v              v             v
   pipeline      render_engine   shorts_        viral_
   .py           .py             pipeline.py    pipeline.py
       |              |              |             |
       |              |              |             +--> librosa (beats)
       |              |              +--> call_free_model (destaques)
       |              |                  |
       |              |                  v
       |              |             provider_registry.py --> OpenRouter :free
       |              |                  |
       |              |                  +-- sem chave/quota --> fallback local
       |              v
       |          npx --yes hyperframes@0.8.92 render
       |              |
       |              +--> Chrome headless  (frames)
       |              +--> ffmpeg / ffprobe  (analise)
       |
       +--> Pexels / Pixabay / OpenRouter
       +--> asset_quality.py --> ffmpeg signalstats

Fluxo de uma requisicao de video, em ordem:

    HTTP POST /api/build-video
      -> sanitize_name, existe .srt?          (400 se nao)
      -> parse_srt_to_segments
      -> build_storyboard_from_segments       (+ normalize_scene_timings)
      -> search_media_for_scenes              (IA ou heuristica; cache por keyword)
      -> search_media_for_keywords            (pool de recurso)
      -> build_edit_plan_from_segments
      -> render_video_hyperframes
           -> create_project_dir              (apaga e recria .hf_<nome>)
           -> stage_project_assets            (download + cache + rejeicao de padding)
           -> generate_composition_html       (HTML / CSS / GSAP)
           -> render_with_hyperframes         (subprocess npx)
           -> cleanup_render_dirs             (remove os .hf_* antigos)
           -> escreve outputs/<nome>.json
      -> JSON com storyboard, edit_plan, media, render

---

## 6. Pontos fracos conhecidos

**O `requirements.txt` esta incompleto.** `viral_pipeline.py` importa `librosa` e `numpy` no topo, e `backend/app.py:33` importa esse modulo no topo. Nenhum dos dois esta no `requirements.txt`. Uma instalacao limpa com `pip install -r requirements.txt` falha no arranque com `ModuleNotFoundError: No module named 'librosa'`. O `numpy` acaba por chegar por via do `faster-whisper`, mas o `librosa` nao chega por lado nenhum.

**O build Docker falha tal como o repositorio esta.** `Dockerfile:333` corre, no fim do build, `python -c "import fastapi, uvicorn, numpy, soundfile, librosa, edge_tts"`, e nenhum desses quatro ultimos pacotes esta no `requirements.txt`. O comentario no codigo explica a intencao — *"um pacote obrigatorio em falta no requirements.txt tem de fazer o BUILD falhar"* — e e exactamente o que acontece, no ultimo passo, depois de todo o trabalho caro. Some-se a isto o `Dockerfile:283`, que copia uma pasta `scripts/` sem um unico ficheiro versionado. Corrigir o `requirements.txt` resolve os dois de uma vez.

**Nao ha verificacao de ambiente no arranque nativo.** O `scripts/check_env.py` a que o `entrypoint.sh:71` chama nao esta nesta copia (a pasta `scripts/` esta vazia, e `git ls-files scripts` nao devolve nada). O entrypoint trata o caso sem quebrar — avisa e verifica so os quatro binarios — mas em modo nativo nao ha preflight nenhum: um ambiente incompleto descobre-se quando o render falha. Note-se a assimetria: o caminho nativo, que e o caminho de referencia do projecto, e o unico sem preflight.

**`DARK_STUDIO_ALLOWED_ORIGINS` nao e lida por ninguem.** O `docker-compose.yml:48` passa-a ao container e o comentario ao lado descreve com detalhe como a usar para acesso remoto. O `backend/app.py:46` tem as origens CORS escritas no codigo e nao le nenhuma variavel de ambiente. A documentacao do compose descreve um mecanismo que nao existe.

**`AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION` tambem nao sao lidas**, e `edge_tts` aparece na sanidade de build do `Dockerfile` sem que exista modulo de TTS. Sao tres rastos do mesmo projecto — o de TTS — que nao chegou a esta copia.

**`librosa` e um custo de arranque.** O primeiro `import librosa` compila e cacheia o numba e demora varios segundos.

**A transcricao degrada em silencio.** `transcribe_audio_file` engole qualquer excecao do `faster-whisper` e devolve cinco frases fixas em portugues, com 3 segundos cada. Um SRT errado nao da erro em lado nenhum: `/api/transcribe` responde `200` e `/api/build-video` renderiza um video confiavelmente errado. E o defeito mais facil de nao detectar de todo o projecto.

**A porta esta fixa em 8013 no arranque nativo.** Nem `start.bat` nem `start.sh` leem `DARK_STUDIO_PORT` nem procuram uma porta livre acima dela. Se a 8013 estiver ocupada, o arranque falha. Dentro do container a variavel funciona, porque o `entrypoint.sh:40` a le — mas o `ports: "8013:8013"` do compose esta escrito a mao, entao as duas tem de mudar em conjunto.

**A WebUI e um unico ficheiro.** `frontend/index.html` tem 112 linhas com o CSS e o JavaScript todos inline, minificados, e carrega apenas `auth-handler.js`. Nao ha build step nem separacao de componentes. Para uma aplicacao destined a crescer, e a divida tecnica mais visivel de toda a base de codigo — e a raza de o `auth-handler.js` ser, apesar do nome, a unica unidade de logica de cliente extraida.

**A origem CORS esta fixa no codigo.** `backend/app.py:46` permite `http://127.0.0.1:8013` e `http://localhost:8013`, sem regex e sem ler variavel de ambiente. O atalho `Abrir Dark Video Studio.bat` serve a interface na porta 8080 — uma origem que o backend bloqueia — e nao mostra janela do servidor.

**`Abrir Dark Video Studio.bat` usa `Get-NetTCPConnection`, que precisa do modulo NetTCPIP.** Em um Windows onde esse modulo nao esteja carregado, o comando falha e o script sai sem arrancar nada, sem mensagem de erro.

**A memoria de quota vive no processo.** `_OPENROUTER_QUOTA` (`provider_registry.py:669`) e um dicionario em memoria. Reiniciar limpa o estado, e `GET /api/providers` mostra `null` ate haver uma chamada real ao OpenRouter.

**O cache de media e por processo.** `_MEDIA_CACHE` (`pipeline.py:444`) vive em memoria e nunca e invalidado. Numa sessao longa, uma fotografia que o fornecedor deixou de servir continua a ser servida a partir do cache.

**Os testes `live` correm por omissao.** `pytest.ini` declara os marcadores mas nao os desmarca. Um `pytest` sem argumentos inclui `tests/test_live_providers.py`; cada teste salta se a chave correspondente nao existir, mas com chaves configuradas isso e uma chamada real a uma API por teste, a gastar a quota diaria do OpenRouter.

**`tests/test_e2e.py` importa `requests`, que nao esta no `requirements.txt`.** Alem disso, qualquer teste que importe `backend.app` precisa de `librosa` instalado.

**O `.venv` versionado e um venv do Windows.** A pasta `.venv/` na raiz tem `Lib/`, `Scripts/` e `Include/` — e um virtualenv Windows, inutilizavel em Linux ou macOS.

**A WebUI chama a API com caminhos relativos.** `frontend/index.html:32` define `const apiBase = ''`, com o comentario *"Served from the same origin as the API, so a relative base works on any port and no CORS preflight is involved"*. E a decisao certa: dispensa a configuracao de `apiBase` e dispensa preflight. A contrapartida e que a interface so funciona servida pelo proprio backend, em `/app/`. O atalho `Abrir Dark Video Studio.bat`, que a serve com `python -m http.server 8080`, quebra todas as chamadas — e e o unico caminho que o projecto oferece para ter a interface sem a janela do servidor visivel.
