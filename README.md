# Dark Studio

**PT.** O Dark Studio é uma mesa de montagem de vídeo narrado que corre inteiramente na sua máquina. Escreve um tema, o servidor trata do resto: gera o guião, sintetiza a voz, transcreve o áudio para legendas, pesquisa imagem de stock cena a cena e devolve um MP4 vertical com legendas queimadas, cortes e música de fundo. A interface é uma SPA servida pelo próprio backend em `/app/`; a IA é sempre um modelo **`:free`** do OpenRouter, e a inexistência de qualquer parâmetro que peça um modelo pago é uma invariante do projecto, não um acidente.

Documentação: **[Instalação](docs/INSTALL.md)** · **[Arquitectura](docs/ARCHITECTURE.md)** · **[API](docs/API.md)**

---

## Arranque em Windows, com um clique

1. Instale o que falta: **Python 3.10+**, **ffmpeg** (no `PATH`) e **Node.js LTS**. As instruções estão em [docs/INSTALL.md](docs/INSTALL.md).
2. Copie `.env.example` para `.env` e preencha, no mínimo, `OPENROUTER_API_KEY`.
3. **Duplo clique em `Abrir Dark Video Studio.bat`.**

Esse atalho não faz nada por si — `Abrir Dark Video Studio.bat:9` limita-se a chamar `start.bat`. É o `start.bat` que faz o trabalho, em quatro passos:

| Passo | O que faz | Onde |
| --- | --- | --- |
| 1/4 | Procura o Python: primeiro o `.venv` do projecto (e testa se executa), depois `py -3`, depois `python`. | `start.bat:66-75` |
| 2/4 | Corre `scripts/check_env.py --fix`, que valida Python, pacotes, `ffmpeg`/`ffprobe`/`node`/`npx`, escrita em `storage/` e presença das chaves. Sai com código 1 se faltar algo obrigatório. | `start.bat:94` |
| 3/4 | Escolhe a porta: `DARK_STUDIO_PORT` (por omissão 8013) e, se estiver ocupada, a primeira livre até `DARK_STUDIO_PORT_MAX`. | `start.bat:111-116` |
| 4/4 | Arranca `uvicorn backend.app:app` em `127.0.0.1` e, 4 segundos depois, abre o navegador em `/app/`. | `start.bat:151`, `start.bat:154` |

O mesmo, em texto:

```bat
set DARK_STUDIO_PORT=9000
start.bat
```

`DARK_STUDIO_NO_BROWSER=1` impede a abertura automática do navegador.

Em Linux e macOS, `sh start.sh` faz exactamente o mesmo, com a mesma sonda de portas (`start.sh:65`) e o mesmo verificador (`start.sh:51`).

> **A raiz não serve a interface.** O backend monta `frontend/` em `/app` (`backend/app.py:114-116`) e **não existe rota em `/`** — `http://127.0.0.1:8013/` devolve `404`, verificado em execução. Use sempre `http://127.0.0.1:<porta>/app/`. Os atalhos já abrem o endereço certo; se escrever o URL à mão, é aqui que se falha.

---

## O fluxo de um clique

`POST /api/generate` (em [`docs/API.md`](docs/API.md#10-post-apigenerate)) arranca o tema inteiro e devolve `202` com um `job_id`. O orquestrador é `backend/services/generator.py` (715 linhas), que conduz quatro etapas e grava o progresso em marcos públicos:

| Etapa | Progresso | O que acontece |
| --- | --- | --- |
| `script` | `0.05` | `script_gen.generate_script` — modelo `:free` ou guião local de recurso |
| `voice` | `0.20` | `tts.synthesize_speech_long`, `pipeline.transcribe_audio_file` e escrita do SRT |
| `media` | `0.40` | `pipeline.search_media_for_scenes` — uma pesquisa por cena |
| `render` | `0.60` | `render_engine.render_video_hyperframes` — HyperFrames via `npx` |
| `done` | `1.00` | resultado anexado ao job; `status` passa a `completed` |

Os valores vivem em constantes nomeadas (`generator.py:53-57`) porque são contrato público: quem os consulta não deve adivinhar.

No máximo **dois** jobs correm ao mesmo tempo (`MAX_CONCURRENT_JOBS = 2`, `generator.py:45`); os restantes ficam em fila atrás de um semáforo. Qualquer falha de etapa é apanhada, registada como `status: "failed"` com mensagem em português, e a excepção é engolida — um job falhado nunca bloqueia a fila nem propaga para quem o chamou.

### Dois avisos honestos sobre este fluxo

- **Acompanhar um job pela WebUI é lento por desenho.** O painel faz *poll* a `GET /api/jobs/{job_id}` de 1,5 em 1,5 segundos (`frontend/js/generator.js:244`). Para scripting, use a API directamente — o histórico completo está em `GET /api/jobs`, que devolve um **array simples**, não um objecto com envelope:

  ```bash
  curl -s -X POST http://127.0.0.1:8013/api/generate \
       -H 'Content-Type: application/json' \
       -d '{"topic":"a história do café em Portugal","section_count":5}'
  curl -s http://127.0.0.1:8013/api/jobs/<job_id>
  ```

- **`DELETE /api/jobs/{job_id}` só cancela o que ainda está em fila.** Um job `queued` passa a `cancelled`; um job `running` devolve `{"cancelled": false}` e deixa o render acabar. Ver [Limitações conhecidas](#limitações-conhecidas), ponto 11.

O fluxo manual — guião → voz → legendas → composição → render — funciona todo, e é o caminho que a WebUI usa por baixo. Ver [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#3-o-pipeline-etapa-a-etapa).

---

## Funcionalidades

Cada linha aponta para o ficheiro e o símbolo que a implementam.

**Conteúdo e texto**
- **Transcrição de áudio para SRT** — `pipeline.transcribe_audio_file` (`pipeline.py:64`) usa `faster-whisper` (modelo `tiny`, CPU, `int8`) e escreve `storage/uploads/<projeto>.srt`; exposto em `POST /api/transcribe`.
- **Geração de guião** — `script_gen.generate_script` (`script_gen.py:480`) em cinco idiomas (`script_gen.LANGUAGES`, `script_gen.py:32`), com recurso local declarado em `source: "fallback-local"` mais `fallback_error` quando o modelo não está disponível.
- **Extracção de termos de pesquisa** — `pipeline.extract_keywords_from_text` (`pipeline.py:176`) descarta a estrutura do SRT e as stopwords portuguesas, penaliza flexões vagas e intercala bigramas com palavras soltas.
- **Destaques automáticos para shorts** — `shorts_pipeline.detect_highlights` (`shorts_pipeline.py:48`); sem chave ou sem quota, uma heurística por comprimento e espalhamento devolve sempre alguma coisa.
- **Legendas karaoke por palavra** — `shorts_pipeline.extract_words_with_timestamps` (`shorts_pipeline.py:189`) distribui o tempo de cada segmento pelas palavras, pesando pelo comprimento de cada token.
- **Ângulo editorial de um nicho** — `POST /api/strategy` devolve `angle`, `hook`, três `titles`, `visual_direction` e três `chapters`, com recurso local declarado.

**Imagem**
- **Stock media Pexels e Pixabay** — `pipeline.fetch_provider_media` (`pipeline.py:593`).
- **Pesquisa de media por cena, com termos escritos pelo modelo** — `pipeline.extract_visual_terms_with_ai` (`pipeline.py:483`) pede ao modelo termos por cena; sem ele, `pipeline.extract_scene_keywords` (`pipeline.py:227`) extrai da legenda da própria cena. `pipeline.search_media_for_scenes` (`pipeline.py:549`) junta as duas.
- **Rejeição de imagens com moldura branca** — `asset_quality.looks_padded` (`asset_quality.py:70`) mede com `ffmpeg signalstats` a duas profundidades em cada borda e rejeita o asset, que passa a candidato seguinte. O resultado entra em `render.rejected_assets`.
- **Cache de media por URL** — `pipeline.download_media_asset` (`pipeline.py:668`) guarda em `storage/media/<projeto>/`, com o hash do URL no nome do ficheiro.

**Vídeo**
- **Render HyperFrames via `npx`** — `render_engine.render_with_hyperframes` (`render_engine.py:892`) corre `npx --yes hyperframes@0.8.92 render`, que lança Chrome headless.
- **Composição HTML com GSAP** — `render_engine.generate_composition_html` (`render_engine.py:483`) emite uma timeline por cena: fade, wipe de entrada, Ken Burns e desfoque com recuo do lado que sai.
- **Grain e barra de progresso** — `_grain_overlay` (`render_engine.py:252`) e `_progress_bar` (`render_engine.py:271`) usam passos absolutos em tempos absolutos, não `infinite`, por isso o resultado é idêntico em qualquer frame.
- **Storyboard com sobreposição de 0,7 s** — `pipeline.normalize_scene_timings` (`pipeline.py:319`) faz cada cena começar antes do seu tempo de origem, para o wipe ter o que revelar; os fins ficam na timeline de origem, para a narração ficar sincronizada.
- **Assets copiados para dentro da composição** — `render_engine.stage_project_assets` (`render_engine.py:370`) copia tudo para `storage/outputs/.hf_<nome>/assets/`, porque o HyperFrames recusa recursos locais fora do directório do projecto.
- **Cortes ao ritmo (beat-sync), loop contínuo, thumbnail e metadados de plataforma** — `viral_pipeline.detect_beats` (`viral_pipeline.py:40`), `make_seamless_loop` (`viral_pipeline.py:148`), `generate_optimized_thumbnail` (`viral_pipeline.py:174`) e `build_platform_metadata` (`viral_pipeline.py:221`). Exposto em `POST /api/build-viral`.
- **Formatos** — `vertical` 1080x1920, `square` 1080x1080, `landscape` 1920x1080 (`render_engine.get_dimensions`, `render_engine.py:233`).
- **Streaming dos MP4** — `GET /api/project/{nome}/video` e `GET /api/project/{nome}/shorts/video` devolvem `video/mp4` directamente, com `404` (e não JSON) para que um `<video>` possa falhar de forma visível.
- **Música de fundo misturada depois do render** — `render_engine.mix_audio_track` (`render_engine.py:1072`) volta a correr `ffmpeg` sobre o MP4 já produzido. Nunca é fatal: uma faixa em falta ou um `ffmpeg` em falta saem em `music.applied` falso, com uma nota a explicar porquê.

**Confiança e operação**
- **Contrato `AUTH_*` uniforme** — `auth_contract.AuthError` (`auth_contract.py:14`) transporta `error_code`, `message`, `details` e `status_code`; o handler instalado em `backend/app.py:81-84` devolve-o tal e qual. São **seis** códigos, definidos em `auth_contract.py:6-11`.
- **Tratamento do erro no navegador** — `frontend/auth-handler.js` expõe `window.AuthHandler`: reenvia com backoff os códigos retentáveis, redirecciona para `/#settings` os de chave em falta ou rejeitada, e mostra um toast nos restantes.
- **Só modelos `:free`** — verificado em três camadas: `POST /api/settings` recusa com `400` um modelo sem `:free`, `provider_registry._call_openrouter_compat` (`provider_registry.py:728`) volta a verificar antes de cada chamada, e não existe parâmetro nenhum que permita pedir um modelo pago.
- **Multi-fornecedor** — `provider_registry.ProviderRegistry` (`provider_registry.py:174`) configura OpenRouter, NVIDIA, OpenCode, Gemini e OpenAI (`provider_registry.py:93`), com memória de quota lida dos cabeçalhos `x-ratelimit-*` (`provider_registry.py:685`).
- **53 vozes `edge-tts` sem chave** — `tts.EDGE_VOICES` (`tts.py:133`), filtráveis por locale em `GET /api/voices`. OpenAI e Azure são alternativas opcionais (`tts.SUPPORTED_PROVIDERS`, `tts.py:101`).
- **8 temas de estilo e 6 ambientes musicais** — `style._PRESET_LIST` (`style.py:412`) e `music.MOODS` (`music.py:52`); `music.BUILTIN_SPECS` (`music.py:98`) descreve 6 faixas sintetizadas localmente.
- **WebUI servida pelo backend** — `backend/app.py:114-116` monta `frontend/` em `/app` com `html=True`.
- **Docker** — `Dockerfile` de três estágios (`browser` com Chrome via Puppeteer, `builder` com o virtualenv, `runtime`), utilizador sem privilégios (uid 10001), volume nomeado para `storage/`, `init: true` e um `HEALTHCHECK`. Ver [docs/INSTALL.md#5-docker](docs/INSTALL.md#5-docker).

---

## Pré-requisitos

| Precisa de | Versão | Para que serve | Obrigatório? |
| --- | --- | --- | --- |
| Python | 3.10+ | servidor e serviços | Sim |
| `ffmpeg` + `ffprobe` | qualquer actual | medir media, misturar música | Sim, para gerar vídeo |
| `node` + `npx` | LTS (20+) | correr o HyperFrames | Sim, para gerar vídeo |
| Pacotes Python | `requirements.txt` | ver o ficheiro | Sim |
| Chaves de API | — | guião, estratégia, stock media | Não; sem elas há recurso local |

O verificador classifica `faster-whisper` como **opcional** (`scripts/check_env.py:50`) e `librosa` como **obrigatório** (`scripts/check_env.py:48`). Os dois binários externos são classificados como opcionais, mas sem eles a geração de vídeo falha a meio — o servidor arranca na mesma.

---

## Visita rápida à API

Base: `http://127.0.0.1:<porta>`. São **27 rotas** escritas à mão, mais as quatro rotas de documentação que o FastAPI gera sozinho. A referência completa está em [docs/API.md](docs/API.md).

```bash
# Saúde
curl -s http://127.0.0.1:8013/health

# Catálogos (nunca falham: devolvem listas vazias em vez de erro)
curl -s http://127.0.0.1:8013/api/presets          # 8 temas + vocabulários de legendas
curl -s http://127.0.0.1:8013/api/voices            # 53 vozes edge-tts
curl -s http://127.0.0.1:8013/api/languages        # pt-PT, pt-BR, en-US, es-ES, fr-FR
curl -s http://127.0.0.1:8013/api/music/tracks     # biblioteca musical + 6 ambientes

# Fluxo de um clique
curl -s -X POST http://127.0.0.1:8013/api/generate \
     -H 'Content-Type: application/json' \
     -d '{"topic":"a história do café em Portugal"}'
curl -s http://127.0.0.1:8013/api/jobs
curl -s http://127.0.0.1:8013/api/jobs/<job_id>

# Fluxo manual
curl -s -X POST http://127.0.0.1:8013/api/script \
     -H 'Content-Type: application/json' \
     -d '{"topic":"a história do café em Portugal","project_name":"cafe"}'
curl -s -X POST http://127.0.0.1:8013/api/tts \
     -H 'Content-Type: application/json' \
     -d '{"project_name":"cafe"}'
curl -s -X POST http://127.0.0.1:8013/api/build-video \
     -F project_name=cafe -F aspect_ratio=vertical

# Chaves de API, como faz a vista de definições (escreve o .env no disco)
curl -s -X POST http://127.0.0.1:8013/api/settings \
     -H 'Content-Type: application/json' \
     -d '{"openrouter_api_key":"...","openrouter_model":"meta-llama/llama-3.3-8b-instruct:free"}'
```

A documentação interactiva do FastAPI está em `/docs`. Cuidado: descreve os modelos Pydantic das rotas antigas, mas **não** descreve `/api/generate`, `/api/jobs` nem `/api/jobs/{job_id}` — o corpo de `/api/generate` é um `Dict[str, Any]` cru, lido campo a campo do dataclass `GenerationRequest` (`backend/app.py:789-791`).

---

## Estado honesto

O que funciona: o fluxo manual completo, do guião ao MP4; o fluxo de um clique pela API; os catálogos; o contrato `AUTH_*`; o verificador de ambiente; os atalhos de arranque; o contentor.

O que **não** funciona, ou funciona pior do que parece:

### Limitações conhecidas

1. **`GET /api/jobs` devolve um array simples, não um envelope.** É a única rota que responde com `[...]` em vez de `{...}`. Um cliente que leia `resposta.jobs` obtém `undefined` em silêncio. O próprio frontend tem de se guardingar disso (`frontend/js/views/projects.js:155`). Escreva `for (const job of await r.json())`.
2. **A transcrição degrada em silêncio.** Sem `faster-whisper`, ou quando o modelo não devolve segmentos, `pipeline.transcribe_audio_file` (`pipeline.py:64`) engole a excepção e devolve **cinco frases fixas em português**, cada uma de 3 segundos (`pipeline.py:84-94`). A resposta HTTP é `200` e não há campo de aviso. Um `.srt` inventado é exactamente o que `/api/tts` e `/api/build-video` vão consumir a seguir. Verificado: um MP3 de 3 bytes produz `200` com o SRT de recurso.
3. **A quota diária dos modelos `:free` degrada para recurso local, sem falhar.** `_unavailable_reason` (`script_gen.py:146`) salta o pedido e `generate_script` devolve o guião de recurso com `source: "fallback-local"` e `fallback_error`. O `POST /api/script` marca `degraded` verdadeiro, mas **devolve `200`** — se ninguém ler o campo, a degradação é invisível. Para a tornar visível: `GET /api/providers` devolve `openrouter.quota_exhausted`.
4. **Não existe autenticação em lado nenhum.** Não há login, não há palavra-passe, não há token. O `AuthHandler` do frontend trata de *chaves de fornecedores de terceiros*, não de acesso à aplicação. O backend escuta em `127.0.0.1` e a WebUI é uma app local de utilizador único. **Não exponha esta porta a uma rede.**
5. **As preferências vivem só no `localStorage`.** `state.js` persiste `project`, `voice`, `style`, `music` e `preset` na chave `darkstudio.prefs.v1` (`frontend/js/state.js:9`, `frontend/js/state.js:75`). Não há armazenamento de projectos no servidor: `GET /api/projects` (`backend/app.py:1013`) lista **ficheiros** de `storage/uploads/`, não projectos; é o frontend que os agrupa por nome em `frontend/js/projects.js:13`.
6. **As caches são por processo.** O cache de media (`pipeline._MEDIA_CACHE`, `pipeline.py:444`), o de quota (`provider_registry._OPENROUTER_QUOTA`, `provider_registry.py:669`) e o de qualidade de assets (`asset_quality._VERDICT_CACHE`, `asset_quality.py:67`) são dicionários ao nível do módulo. Com mais do que um processo de servidor, não são partilhados.
7. **Os testes `live` só gastam quota quando se pedem.** O `pytest.ini` já traz `addopts = -m "not live"` (`pytest.ini:21`), por isso um `pytest` a seco não chama a OpenRouter nem a Pexels. Os 9 testes de `tests/test_live_providers.py` só entram com `pytest -m live` (`pytest.ini:24`), e nesse caso batem a sério na rede e gastam quota real.
8. **O override de GPU não acelera o render.** `docker-compose.gpu.yml` só dá CUDA ao `faster-whisper`. O Chrome corre com `--disable-gpu` e o encoder de saída é o `ffmpeg`, não o Chrome — está escrito no próprio ficheiro (`docker-compose.gpu.yml:14`). Para renders mais rápidos: mais CPU e mais RAM.
9. **Dois testes de ponta a ponta expiram aos 900 s.** `tests/test_e2e.py:213` e `tests/test_e2e.py:221` fazem `POST /api/build-video` com `BUILD_VIDEO_TIMEOUT = 900` (`tests/test_e2e.py:25`). Numa máquina mais lenta, um render completo pode não caber.
10. **O `requirements.txt` afirma algo que o código já não faz.** As linhas 18-22 dizem que `backend/app.py` importa `viral_pipeline` no topo e que «sem estes pacotes o servidor NAO arranca de todo». Isso foi verdade e deixou de ser: o import é **preguiçoso**, dentro de `build_viral` (`backend/app.py:745`), precisamente para o servidor não depender do `librosa`. O `scripts/check_env.py:48` ainda marca o `librosa` como obrigatório — mantive-o, porque `/api/build-viral` devolve `503` sem ele —, mas o motivo está errado.
11. **O render não é cancelável.** Não existe interruptor partilhado, portanto um job `running` não pára a meio: `cancel_job` recusa-o e o HyperFrames acaba até ao fim. O que impede um render infinito é o *watchdog* — `DARK_STUDIO_RENDER_TIMEOUT`, 900 s por omissão, lido a cada chamada — que mata a árvore de processos e devolve o job como `failed`, com mensagem em português, em vez de um MP4 truncado. Ver [docs/INSTALL.md](docs/INSTALL.md#15-ficheiro-env).

### O que não foi medido

Não corri a suite de testes nem um render completo neste ambiente: não há rede para o Edge TTS nem cabeçalhos de quota do OpenRouter. O que *foi* verificado em execução: a lista completa de rotas (`app.routes`), as respostas e os códigos de estado de todos os caminhos de erro documentados, os catálogos (53 vozes, 8 presets, 6 faixas, 6 ambientes), o `404` em `/`, o `200` em `/app/`, a forma do corpo `AUTH_*`, o ciclo de vida de um job (`queued` -> `running` -> `completed`, com um terceiro a ficar `queued` à espera de *slot*) e o `cancel_job` a meio, que aceita um `queued` e recusa um `running`. As contagens de linhas são de `wc -l`.

---

## Correr os testes

```bash
python -m pip install -r requirements.txt     # inclui pytest, pytest-asyncio e requests
pytest                                          # o pytest.ini já desmarca os testes live
pytest -m "not live"                            # a forma explícita do mesmo
pytest -m "not live and not slow"               # salta também os renders completos
pytest -m live                                  # os 9 testes live: rede real, quota real
```

`pytest.ini` define `asyncio_mode = auto`, `testpaths = tests` e `addopts = -m "not live"`. Os marcadores disponíveis são `slow` e `live`; os 9 testes `live` de `tests/test_live_providers.py` só entram com `pytest -m live`.

---

## Estrutura

```
Dark-Studio/
├── backend/
│   ├── app.py                    1048 linhas · 27 rotas FastAPI
│   └── services/                 pipeline, gerador, TTS, guião, estilo, música,
│                                 render, shorts, viral, fornecedores, auth, qualidade
├── frontend/                     SPA de módulos ES
│   ├── index.html                64 linhas · casca da aplicação
│   ├── styles.css                1394 linhas
│   ├── auth-handler.js           299 linhas · contrato AUTH_* no navegador
│   └── js/                       api, state, ui, catalog, projects, generator,
│                                 composition, main + pickers/ + views/
├── scripts/check_env.py          441 linhas · verificador de ambiente
├── storage/                      uploads, outputs, media, thumbnails, music
├── tests/                        16 ficheiros
├── docs/                         INSTALL, ARCHITECTURE, API
├── Dockerfile, .dockerignore
├── docker-compose.yml, docker-compose.gpu.yml, docker/entrypoint.sh
├── start.bat, start.sh, Abrir Dark Video Studio.bat
└── requirements.txt, pytest.ini, .env.example
```
