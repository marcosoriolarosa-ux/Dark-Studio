# API — Dark Studio

Referencia completa dos endpoints expostos por `backend/app.py` (FastAPI).

- **Base URL:** `http://127.0.0.1:8013` (porta fixa; ver [Portas e origem](#portas-e-origem)).
- **Documentacao interativa gerada pelo proprio servidor:** `http://127.0.0.1:8013/docs` (Swagger) e `/redoc`.
- **Interface web servida pelo backend:** `http://127.0.0.1:8013/app/`.
- Respostas em `application/json` com UTF-8, exceto nos dois endpoints que devolvem video.

Indice:

| # | Metodo | Caminho | Finalidade |
|---|--------|---------|------------|
| 1 | `GET` | `/health` | Sonda de vida do servidor |
| 2 | `POST` | `/api/transcribe` | Audio -> segmentos + SRT |
| 3 | `POST` | `/api/build-video` | Pipeline completo ate MP4 |
| 4 | `POST` | `/api/build-viral` | Pipeline viral (beat-sync, loop, thumbnail) |
| 5 | `POST` | `/api/shorts` | Corte vertical com karaoke |
| 6 | `GET` | `/api/shorts/{project_name}/highlights` | Destaques sem renderizar |
| 7 | `GET` | `/api/media/search` | Pesquisa direta de stock media |
| 8 | `GET` | `/api/providers` | Estado dos fornecedores |
| 9 | `POST` | `/api/strategy` | Angulo editorial de um nicho |
| 10 | `POST` | `/api/settings` | Gravar chaves em `.env` |
| 11 | `POST` | `/api/ai/test` | Teste do modelo `:free` |
| 12 | `GET` | `/api/projects` | Listar ficheiros de projeto |
| 13 | `GET` | `/api/project/{project_name}/video` | MP4 principal |
| 14 | `GET` | `/api/project/{project_name}/shorts/video` | MP4 do short |
| 15 | `GET` | `/app/` | WebUI (ficheiros estaticos) |

Convencoes:

- Nomes de projeto passam por `sanitize_name()` (`backend/app.py:52`), que remove tudo o que nao for `[A-Za-z0-9_-]`. Um nome com acentos, espacos ou pontuacao fica reduzido ao que sobra; um nome vazio torna-se o predefinido do endpoint.
- Erros de validacao devolvem `{"detail": "<mensagem em portugues>"}` com o codigo indicado em cada endpoint.
- Erros de fornecedor devolvem o contrato `AUTH_*` (ver [Contrato `AUTH_*`](#contrato-auth)).
- `Form(...)` significa `multipart/form-data` ou `application/x-www-form-urlencoded`. Os corpos JSON precisam de `Content-Type: application/json`.

---

## 1. `GET /health`

Sonda de vida. Nao toca em disco nem em rede.

Sem parametros.

**200**

    {"status": "ok", "message": "Dark Video Studio MVP running"}

---

## 2. `POST /api/transcribe`

Recebe um audio, transcreve-o e escreve `storage/uploads/<projeto>.srt`.

`multipart/form-data`.

| Campo | Tipo | Predefinicao | Descricao |
|-------|------|---------------|-----------|
| `file` | ficheiro | *(obrigatorio)* | Audio de origem. Sem validacao de formato nem de tamanho. |
| `project_name` | string | `"demo_project"` | Nome do projeto, saneado. |

O conteudo do upload e gravado tal e qual, com a extensao deduzida do nome do ficheiro (`.mp3` se nao houver ponto).

**200**

    {"project_name": "meu_projeto",
     "audio_file": "storage/uploads/meu_projeto.mp3",
     "srt_file":   "storage/uploads/meu_projeto.srt",
     "segments": [{"index": 1, "start": 0.0, "end": 3.2, "text": "..."}]}

> **Atencao — degradacao silenciosa.** `pipeline.transcribe_audio_file` corre o `faster-whisper` dentro de um `try/except` que engole qualquer excecao (`backend/services/pipeline.py:64`). Se o pacote nao estiver instalado, se o modelo nao conseguir descarregar, ou se o audio estiver corrompido, a funcao **nao falha**: devolve sempre as mesmas cinco frases de exemplo em portugues, com timestamps de 3 em 3 segundos. O endpoint responde `200` e o SRT fica errado. A unica forma de saber o que aconteceu e comparar o texto devolvido com o audio.

---

## 3. `POST /api/build-video`

Pipeline principal: SRT -> keywords -> storyboard -> pesquisa de media por cena -> plano de edicao -> composicao HTML -> render HyperFrames.

`multipart/form-data`. Todos os campos sao strings.

| Campo | Tipo | Predefinicao | Descricao |
|-------|------|---------------|-----------|
| `project_name` | string | `"demo_project"` | Precisa de `storage/uploads/<nome>.srt`. |
| `transition` | string | `"fade"` | Transicao aplicada a todas as cenas. |
| `effect` | string | `"cinematic"` | Efeito de movimento por cena. |
| `include_captions` | string | `"true"` | `"true"` = legendas visiveis; qualquer outro valor = `hidden`. |
| `storyboard_json` | string | `""` | Storyboard customizado: JSON com uma lista de objetos. |
| `aspect_ratio` | string | `"vertical"` | `vertical` 1080x1920, `square` 1080x1080, `landscape` 1920x1080. Outro valor cai em `vertical`. |
| `topic` | string | `""` | Nicho, usado como contexto para os termos visuais por IA. |

**200**

    {
      "project_name": "meu_projeto",
      "keywords": ["economia", "papel moeda"],
      "media": [{"source": "Pexels", "provider": "pexels", "title": "economia",
                 "url": "https://images.pexels.com/...", "kind": "image",
                 "width": 1880, "height": 1253, "keyword": "economia"}],
      "scene_media": {"0": []},
      "segments": [],
      "storyboard": [],
      "edit_plan": [],
      "scene_count": 5,
      "scenes_with_media": 4,
      "term_source": "ai" | "heuristic" | "none",
      "provider_status": {},
      "render": {},
      "render_real": {}
    }

`render` e `render_real` sao **o mesmo objeto**, nao uma copia. O objeto de render traz:

    {"project_name": "...", "output_stem": "...", "status": "rendered" | "error",
     "srt_path": "...", "output_path": "...", "storyboard": [], "scene_count": 5,
     "scenes_with_media": 4, "rejected_assets": [], "aspect_ratio": "vertical",
     "resolution": "1080x1920", "audio_duration": 42.1, "video_duration": 42.1,
     "message": "...", "render_engine": "hyperframes"}

Em caso de falha, `status` passa a `"error"` e entram `error` e `message` no lugar dos campos de sucesso.

**Erros**

| Codigo | Quando |
|--------|--------|
| `400` | Faltou o `.srt` deste projeto |
| `400` | `storyboard_json` nao e JSON valido, nao e uma lista, ou contem algo que nao seja objeto |

O render em si **nunca** devolve erro HTTP: uma falha de ffmpeg ou de node chega num payload com `status: "error"`.

---

## 4. `POST /api/build-viral`

Pipeline viral: deteccao de batidas com `librosa`, cortes alinhados ao ritmo, loop continuo, thumbnail HTML e metadados de plataforma.

`multipart/form-data`.

| Campo | Tipo | Predefinicao | Descricao |
|-------|------|---------------|-----------|
| `project_name` | string | `"viral_project"` | Precisa de `storage/uploads/<nome>.srt`. |
| `aspect_ratio` | string | `"vertical"` | `vertical`, `square`, `landscape`. |
| `include_karaoke` | string | `"true"` | `"false"` desliga o karaoke na primeira cena. |
| `topic` | string | `""` | Tema, para os metadados e a categoria. |
| `niche` | string | `""` | Nicho, para pesquisa de media e categoria. |

**200**

    {"project_name": "meu_projeto", "status": "rendered",
     "output_path": "storage/outputs/meu_projeto_viral.mp4",
     "video_duration": 42.1, "resolution": "1080x1920", "aspect_ratio": "vertical",
     "beat_data": {"tempo": 96.0, "beats": [], "beat_count": 68, "duration": 42.1},
     "platform_metadata": {"hashtags": ["#economia", "#viral"],
                           "category": "Education", "title_options": [],
                           "description": "...", "sound_suggestions": [],
                           "posting_times": []},
     "thumbnail_path": "storage/thumbnails/meu_projeto_thumb.html",
     "loop_duration": 0.5, "hook_duration": 3.0,
     "scenes_with_media": 6, "storyboard": [],
     "render_engine": "hyperframes",
     "message": "Vídeo viral pronto: beat-sync, loop contínuo, thumbnail otimizado, metadados de plataforma."}

O MP4 viral fica em `storage/outputs/<projeto>_viral.mp4`; os metadados em `storage/outputs/<projeto>_viral_meta.json`. O `thumbnail_path` aponta para um **HTML**, nao para um PNG: e um documento para compor, nao uma imagem ja renderizada.

**Erros**

| Codigo | Quando |
|--------|--------|
| `400` | Faltou o `.srt` deste projeto |
| `422` | O pipeline devolveu `status: "error"` |

> `viral_pipeline.py` faz `import librosa` e `import numpy` no topo do modulo, e `backend/app.py:33` importa esse modulo no topo. Ou seja: **sem `librosa` instalado o servidor nao arranca de todo** — nao ha degradacao graceful nesta versao. Ver as [Limitacoes conhecidas](../README.md#limitacoes-conhecidas).

---

## 5. `POST /api/shorts`

Corte vertical com legendas karaoke a partir de um projeto transcrito.

`multipart/form-data`.

| Campo | Tipo | Predefinicao | Descricao |
|-------|------|---------------|-----------|
| `project_name` | string | `"demo_project"` | Precisa de `storage/uploads/<nome>.srt`. |
| `aspect_ratio` | string | `"vertical"` | `vertical`, `square`, `landscape`; qualquer outro valor e normalizado para `vertical`. |
| `include_karaoke` | string | `"true"` | `"false"` remove as palavras e volta a legendas simples. |
| `max_highlights` | int | `3` | Limitado a 1..5. |

**200**

    {"project_name": "meu_projeto", "status": "rendered",
     "output_stem": "meu_projeto_shorts",
     "output_path": "storage/outputs/meu_projeto_shorts.mp4",
     "highlights_count": 3, "scenes": [],
     "aspect_ratio": "vertical", "planned_duration": 12.7,
     "video_duration": 12.7, "resolution": "1080x1920",
     "has_audio": false,
     "audio_note": "corte sem audio: as palavras ja estao nas legendas",
     "karaoke": true,
     "message": "Short pronto: 3 destaques, 12.7s",
     "render_engine": "hyperframes"}

O corte **nao leva a faixa de audio original**: os destaques vem de pontos espalhados da narracao e as palavras ja estao queimadas nas legendas, por isso `has_audio` e `false`.

**Erros**

| Codigo | Quando |
|--------|--------|
| `404` | `SHORTS_NO_SRT` (faltou o `.srt`) ou `SHORTS_NO_SEGMENTS` (SRT vazio) |
| `422` | `SHORTS_NO_HIGHLIGHTS`, ou `status: "error"` do render |

---

## 6. `GET /api/shorts/{project_name}/highlights`

Destaques de um projeto, **sem** renderizar. Usa o mesmo `detect_highlights` do `/api/shorts`.

| Parametro | Tipo | Descricao |
|-----------|------|-----------|
| `project_name` | string (path) | Saneado; `400` se ficar vazio. |

**200** (com SRT)

    {"project_name": "meu_projeto", "status": "ok",
     "highlights_count": 3,
     "highlights": [{"start": 5.2, "end": 8.5, "text": "...", "importance": 0.9}],
     "total_seconds": 12.7}

**200** (sem SRT) — `{"project_name": "...", "status": "error", "error": "SRT file not found."}`. Nesta rota a ausencia de SRT **nao** e `404`.

**400** — nome de projeto invalido.

---

## 7. `GET /api/media/search`

Pesquisa direta num fornecedor de stock media.

| Parametro | Tipo | Predefinicao | Descricao |
|-----------|------|---------------|-----------|
| `query` | string (query) | *(obrigatorio)* | Termo de pesquisa. |
| `provider` | string (query) | `"pexels"` | `pexels` ou `pixabay`. |

**200**

    {"query": "economia", "provider": "pexels",
     "results": [{"source": "Pexels", "provider": "pexels", "title": "economia",
                  "url": "https://images.pexels.com/...", "kind": "image",
                  "width": 1880, "height": 1253}]}

Sem fornecedor configurado, ou com uma pesquisa sem resultados, a resposta e `{"results": []}`. Uma chave rejeitada (401/402/403/429) ou um erro 5xx do fornecedor **nao** e convertido em lista vazia: propaga o contrato `AUTH_*`, para que uma credencial partida nao pareca "esta pesquisa nao tem resultados".

---

## 8. `GET /api/providers`

Estado de cada fornecedor. Sem parametros.

**200**

    {"providers": {"gemini":    {"enabled": false},
                   "openai":    {"enabled": false},
                   "youtube":   {"enabled": false},
                   "pexels":    {"enabled": true},
                   "pixabay":   {"enabled": false},
                   "openrouter": {"enabled": true,
                                  "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                                  "free_only": true,
                                  "quota_remaining": null, "quota_limit": null,
                                  "quota_exhausted": false}}}

Um fornecedor aparece `enabled: true` quando a respetiva chave esta definida no ambiente. `quota_remaining` e `quota_limit` so ficam preenchidos depois de uma chamada real ao OpenRouter que devolva os cabecalhos `x-ratelimit-*`; o estado vive em memoria do processo e nao e persistido.

---

## 9. `POST /api/strategy`

Angulo editorial para um nicho: `angle`, `hook`, `titles` (pelo menos 3), `visual_direction`, `chapters` (pelo menos 3).

**Corpo JSON** (`StrategyRequest`)

| Campo | Tipo | Predefinicao | Descricao |
|-------|------|---------------|-----------|
| `niche` | string | *(obrigatorio)* | Tema ou nicho. Nao pode ficar vazio. |
| `transcript` | string | `""` | Texto disponivel; truncado a 4000 caracteres no prompt. |

**200 — resposta do modelo**

    {"angle": "...", "hook": "...", "titles": [], "visual_direction": "...",
     "chapters": [], "source": "openrouter-free",
     "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"}

**200 — fallback local**

    {"angle": "...", "hook": "...", "titles": [], "visual_direction": "...",
     "chapters": [], "source": "fallback-local", "fallback_error": "<motivo>"}

Indisponibilidade do modelo **nao** e erro HTTP: um fallback local e um sucesso. O `source` diz sempre qual dos dois respondeu e o fallback nunca esconde o motivo. So `niche` vazio devolve `400`.

---

## 10. `POST /api/settings`

Grava as chaves em `.env` na raiz do projecto e atualiza `os.environ` na mesma execucao.

**Corpo JSON** (`ApiSettings`, `backend/app.py:64`) — todos `string`, predefinicao `""`:

`openrouter_api_key`, `openrouter_model`, `pexels_api_key`, `pixabay_api_key`, `gemini_api_key`, `openai_api_key`, `youtube_api_key`.

Regras:

- `openrouter_model` que nao termine em `:free` devolve `400` ("Use apenas um modelo OpenRouter terminado em :free.").
- `openrouter_model` vazio assume `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
- Campos que ficam vazios **nao apagam** a variavel do ambiente: caem para o valor que ja existia (`gemini`, `openai` e `youtube`). Para `OPENROUTER_API_KEY`, `PEXELS_API_KEY` e `PIXABAY_API_KEY` um valor vazio remove mesmo a variavel do processo.
- O `.env` e reescrito por inteiro: apenas estas 7 chaves, sem os comentarios do `.env.example`.

**200** — `{"status": "saved", "providers": { ...mesma forma de GET /api/providers... }}`
**Erros:** `400` para modelo nao-gratuito.

---

## 11. `POST /api/ai/test`

Dispara uma chamada real ao modelo `:free` configurado ("Responda apenas: OK"). Sem corpo.

**200** — `{"status": "ok", "provider": "openrouter", "model": "...:free", "response": "OK"}`

**Erros**

| Codigo | Corpo | Quando |
|--------|-------|--------|
| `200` | `{"status": "error", "provider": ..., "model": ..., "error": "..."}` | Erro nao-autoral: rede, `RuntimeError`, `ValueError` |
| `401` / `403` / `429` / `402` / `502` | `{"status": "error", "error_code": "AUTH_...", "message": ..., "details": {...}}` | Contrato `AUTH_*` |

---

## 12. `GET /api/projects`

Lista os ficheiros de `storage/uploads/`. Sem parametros.

**200** — `{"projects": [{"name": "meu_projeto.srt", "path": "storage/uploads/meu_projeto.srt"}]}`

Nao e um catalogo de projetos: sao ficheiros. O `.srt` e o `.mp3` do mesmo projeto aparecem como duas entradas.

---

## 13. `GET /api/project/{project_name}/video`

Descarrega o MP4 principal de um projeto (`storage/outputs/<nome>.mp4`).

| Parametro | Tipo | Descricao |
|-----------|------|-----------|
| `project_name` | string (path) | Saneado; `400` se ficar vazio. |

**200** — `video/mp4` (stream de ficheiro, nao JSON).
**Erros:** `400` nome invalido; `404` "Vídeo ainda não foi gerado." — 404 e nao 200-com-JSON de proposito, porque um elemento `<video>` nao consegue ler um corpo de erro e falharia em silencio.

---

## 14. `GET /api/project/{project_name}/shorts/video`

Igual ao anterior, para `storage/outputs/<nome>_shorts.mp4`.

**Erros:** `400` nome invalido; `404` "Short ainda não foi gerado."

---

## 15. `GET /app/`

Montagem de ficheiros estaticos de `frontend/` (`backend/app.py:57`). Serve `index.html` em `/app/`, mais `auth-handler.js` e `js/`.

Se a pasta `frontend/` nao existir, o mount nao e feito e `/app/` devolve 404.

---

## Contrato `AUTH_*`

Falhas de fornecedor — de IA ou de media — chegam ao cliente com a mesma forma, definida em `backend/services/auth_contract.py`:

    {
      "error_code": "AUTH_RATE_LIMIT",
      "message": "Rate limit atingido no fornecedor livre. Tente novamente (...).",
      "details": {"provider": "openrouter", "attempts": 3}
    }

`backend/app.py:39` instala um handler de excepcao para `AuthError`, por isso o corpo e o codigo de estado saem sempre juntos: o handler apanha a excepcao e devolve o contrato em vez de o achatar numa string de erro.

| `error_code` | HTTP | Quando | Detalhes tipicos |
|--------------|------|---------|-------------------|
| `AUTH_MISSING_KEY` | `401` | Chave nao configurada | `provider` |
| `AUTH_INVALID_KEY` | `401` ou `403` | Chave rejeitada; preserva o estado devolvido pelo fornecedor | `provider`, `upstream_status` |
| `AUTH_RATE_LIMIT` | `429` | Limite de pedidos | `provider`, `attempts`, `upstream_status` |
| `AUTH_QUOTA_EXCEEDED` | `402` | Creditos ou **quota diaria de modelos `:free`** esgotados | `provider`, `scope: "daily"`, `limit`, `reset_at`, `error_code_hint` |
| `AUTH_MODEL_NOT_FREE` | `400` | Modelo sem `:free` | `model` |
| `AUTH_REQUEST_FAILED` | `502` (ou o estado do fornecedor) | Falha de rede, resposta vazia, 5xx do fornecedor, chaves esgotadas | `provider`, `last_error`, `exception` |

`AuthError` herda de `RuntimeError`, pelo que um `except RuntimeError` ja existente continua a apanhar tudo — mas `error_code` e `status_code` ficam disponiveis para a camada de API.

`AUTH_MODEL_NOT_FREE` e uma invariante do projecto, nao um erro ocasional: `POST /api/settings` recusa um `openrouter_model` sem `:free` com `400` antes sequer de gravar, e `provider_registry._call_openrouter_compat` volta a recusar no momento da chamada. Nao existe nenhum parametro que permita pedir um modelo pago.

### O que a WebUI faz com os codigos

`frontend/auth-handler.js` expoe `window.AuthHandler` e traduz cada codigo numa accao:

| Codigo | Mensagem (pt-PT) | Comportamento |
|--------|------------------|---------------|
| `AUTH_MISSING_KEY` | "Chave de API em falta. Adicione-a nas configurações para continuar." | Redirecciona para `/#settings` |
| `AUTH_INVALID_KEY` | "Chave de API rejeitada. Verifique a chave nas configurações." | Redirecciona para `/#settings` |
| `AUTH_RATE_LIMIT` | "Limite de pedidos atingido. A tentar novamente dentro de momentos." | Reenvio automatico com backoff (ate 3 tentativas, 1 s a 15 s) |
| `AUTH_QUOTA_EXCEEDED` | "Cota da API esgotada. Actualize o plano ou amanhã tente novamente." | Sem reenvio automatico |
| `AUTH_SERVER_ERROR` | "O fornecedor de IA está com problemas. Tente novamente mais tarde." | Reenvio automatico |
| `AUTH_NETWORK_ERROR` | "Sem ligação ao servidor. Verifique a sua rede e tente novamente." | Reenvio automatico |
| `AUTH_TIMEOUT` | "O pedido demorou demasiado. Verifique a ligação e tente novamente." | Reenvio automatico |
| `AUTH_UNKNOWN` | "Ocorreu um erro inesperado. Tente novamente ou contacte o suporte." | Sem reenvio automatico |

`RETRYABLE_CODES` = `AUTH_RATE_LIMIT`, `AUTH_SERVER_ERROR`, `AUTH_NETWORK_ERROR`, `AUTH_TIMEOUT`.
`SETTINGS_REDIRECT_CODES` = `AUTH_MISSING_KEY`, `AUTH_INVALID_KEY`.
Feedback por omissao: `toast`; `banner`, `modal` e `silent` tambem aceites. Um erro do backend que **nao** traga `error_code` cai em `AUTH_UNKNOWN`.

---

## Portas e origem

| Variavel | Onde e lida | Efeito real |
|----------|-------------|-------------|
| `DARK_STUDIO_PORT` | em lado nenhum nesta versao | `start.bat` e `start.sh` tem a porta 8013 fixa no comando do uvicorn e o `backend/app.py` nao le variaveis de porta. Definir a variavel nao muda nada. |
| `DARK_STUDIO_ALLOWED_ORIGINS` | em lado nenhum nesta versao | `backend/app.py:46` tem as origens fixas no codigo. |

### CORS

`backend/app.py:44` permite exactamente duas origens:

    http://127.0.0.1:8013
    http://localhost:8013

Nao ha `allow_origin_regex`. Consequencias praticas:

- A interface tem de ser aberta em `http://127.0.0.1:8013/app/` ou `http://localhost:8013/app/`. Qualquer outra origem — outra porta, `http://[::1]:8013`, o hostname da maquina, um `file://` — e bloqueada no `fetch`.
- O atalho `Abrir Dark Video Studio.bat` serve a interface por um `python -m http.server 8080` separado. A origem passa a ser `http://127.0.0.1:8080`, que **nao** esta na lista: a pagina carrega, mas todas as chamadas a API falham com um erro de CORS na consola. Use `start.bat`.
- O servidor faz bind a `127.0.0.1`, nao a `0.0.0.0`. So a maquina local acede.

---

## Exemplos

Transcrever, montar e descarregar:

    curl -X POST http://127.0.0.1:8013/api/transcribe -F project_name=exemplo -F file=@narracao.mp3
    curl -X POST http://127.0.0.1:8013/api/build-video -F project_name=exemplo -F aspect_ratio=vertical -F transition=fade
    curl -o exemplo.mp4 http://127.0.0.1:8013/api/project/exemplo/video

Estado dos fornecedores e teste do modelo:

    curl http://127.0.0.1:8013/api/providers
    curl -X POST http://127.0.0.1:8013/api/ai/test

Cortar um short e um viral:

    curl -X POST http://127.0.0.1:8013/api/shorts -F project_name=exemplo -F max_highlights=3
    curl -X POST http://127.0.0.1:8013/api/build-viral -F project_name=exemplo -F topic=economia
