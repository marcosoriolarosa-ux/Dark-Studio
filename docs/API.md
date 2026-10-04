# API — Dark Studio

Referencia completa das **27 rotas** escritas à mão em `backend/app.py` (1048 linhas). A elas somam-se as rotas de documentação que o FastAPI gera sozinho (`/docs`, `/redoc`, `/openapi.json`) e a montagem de ficheiros estáticos em `/app/`. Para o desenho por trás das rotas ver [ARCHITECTURE.md](ARCHITECTURE.md); para a instalação ver [INSTALL.md](INSTALL.md).

- **Base URL:** `http://127.0.0.1:<porta>`. A porta não é fixa nem lida pelo servidor: os atalhos escolhem-na a partir de `DARK_STUDIO_PORT` (por omissão 8013) e, se estiver ocupada, a primeira livre até `DARK_STUDIO_PORT_MAX` (`start.bat:111-116`, `start.sh:65`). Ver [Porta e origens](#porta-e-origens).
- **Documentação interativa:** `/docs` (Swagger) e `/redoc`. **Não descrevem `/api/generate` correctamente** — ver [Documentação automática do FastAPI](#documentação-automática-do-fastapi).
- **Interface web servida pelo backend:** `http://127.0.0.1:<porta>/app/`.
- Respostas em `application/json` com UTF-8, excepto os dois endpoints que devolvem vídeo (`video/mp4`).

> **A raiz não serve a interface — é a confusão número um na primeira utilização.** O backend monta a pasta `frontend/` em `/app` (`backend/app.py:114-116`, com `html=True`), e **não existe rota em `/`**: `http://127.0.0.1:<porta>/` devolve `404`, verificado em execução. A URL da aplicação é sempre `http://127.0.0.1:<porta>/app/`. Os atalhos já abrem o endereço certo; se escrever o URL à mão, é aqui que se falha.

Índice:

| # | Método | Caminho | Finalidade |
| --- | --- | --- | --- |
| 1 | `GET` | `/health` | Sonda de vida do servidor |
| 2 | `GET` | `/api/languages` | Idiomas do gerador de guiões |
| 3 | `GET` | `/api/voices` | Catálogo de vozes |
| 4 | `GET` | `/api/tts/status` | Fornecedores de narração disponíveis |
| 5 | `GET` | `/api/presets` | Temas e vocabulários de legendas |
| 6 | `GET` | `/api/music/tracks` | Biblioteca musical |
| 7 | `GET` | `/api/music/search` | Pesquisa na biblioteca musical |
| 8 | `POST` | `/api/music/upload` | Carregar um ficheiro de música |
| 9 | `GET` | `/api/providers` | Estado dos fornecedores |
| 10 | `POST` | `/api/generate` | Fluxo de um clique: tema -> vídeo |
| 11 | `GET` | `/api/jobs` | Histórico de jobs (array simples) |
| 12 | `GET` | `/api/jobs/{job_id}` | Um job |
| 13 | `DELETE` | `/api/jobs/{job_id}` | Cancelar um job na fila |
| 14 | `POST` | `/api/script` | Tema -> guião |
| 15 | `POST` | `/api/tts` | Texto -> MP3 + SRT |
| 16 | `POST` | `/api/transcribe` | Áudio -> segmentos + SRT |
| 17 | `POST` | `/api/build-video` | Pipeline manual completo até MP4 |
| 18 | `POST` | `/api/build-viral` | Pipeline viral (beat-sync, loop, thumbnail) |
| 19 | `POST` | `/api/shorts` | Corte vertical com karaoke |
| 20 | `GET` | `/api/shorts/{project_name}/highlights` | Destaques sem renderizar |
| 21 | `POST` | `/api/strategy` | Ângulo editorial de um nicho |
| 22 | `POST` | `/api/settings` | Gravar chaves em `.env` |
| 23 | `POST` | `/api/ai/test` | Teste do modelo `:free` |
| 24 | `GET` | `/api/media/search` | Pesquisa directa de stock media |
| 25 | `GET` | `/api/projects` | Ficheiros de `storage/uploads/` |
| 26 | `GET` | `/api/project/{project_name}/video` | MP4 principal |
| 27 | `GET` | `/api/project/{project_name}/shorts/video` | MP4 do short |

Convenções:

- Nomes de projecto passam por `sanitize_name()` (`backend/app.py:111-112`), que remove tudo o que não for `[A-Za-z0-9_-]`. Um nome com acentos, espaços ou pontuação fica reduzido ao que sobra; um nome vazio torna-se o predefinido do endpoint, ou `400` nas rotas que o exigem.
- Erros de validação devolvem `{"detail": "<mensagem em português>"}` com o código indicado em cada endpoint.
- Erros de fornecedor devolvem o contrato `AUTH_*` (ver [Contrato `AUTH_*`](#contrato-auth_)).
- `Form(...)` significa `multipart/form-data`. Os corpos JSON precisam de `Content-Type: application/json`.
- Os modelos Pydantic das rotas JSON vivem em `backend/app.py:119-160`. O `ProjectRequest` (`backend/app.py:119-120`) está declarado mas **não é usado por nenhuma rota**.

---

## 1. `GET /health`

Sonda de vida. Não toca em disco nem em rede. Sem parâmetros.

**200** — `{"status": "ok", "message": "Dark Video Studio MVP running"}`

Handler `health`, `backend/app.py:286-288`.

---

## 2. `GET /api/languages`

Idiomas que o gerador de guiões aceita, para o selector da interface. Sem parâmetros.

**200**

```json
{
  "languages": {
    {"code": "pt-PT", "label": "Portuguese (Portugal)"},
    {"code": "pt-BR", "label": "Portuguese (Brazil)"},
    {"code": "en-US", "label": "English (United States)"},
    {"code": "es-ES", "label": "Spanish (Spain)"},
    {"code": "fr-FR", "label": "French (France)"}
  },
  "default": "pt-PT"
}
```

Os rótulos vêm de `script_gen.LANGUAGES` (`script_gen.py:32-38`). Nunca falha: uma excepção no serviço degrada para uma lista vazia com a chave `error` (`backend/app.py:291-297`).

---

## 3. `GET /api/voices`

Catálogo de vozes, opcionalmente filtrado por prefixo de locale (query `locale`). Um locale sem correspondência devolve uma lista vazia, não um erro.

**200**

```json
{
  "voices": {
    {"id": "pt-PT-RicardoMultilingualNeural", "name": "Ricardo", "gender": "male", "locale": "pt-PT", "provider": "edge"}
  },
  "default": "pt-PT-RicardoMultilingualNeural",
  "locale": "",
  "providers": {"edge", "openai", "azure"}
}
```

São 53 vozes `edge-tts` sem chave (`tts.EDGE_VOICES`, `tts.py:133`); cada entrada tem os campos do dataclass `VoiceInfo` (`tts.py:119-124`). Uma falha no serviço degrada para `"voices": []` (`backend/app.py:348-364`).

---

## 4. `GET /api/tts/status`

Que fornecedores de narração podem correr agora. **Sempre `200`**: um pacote opcional em falta é uma capacidade degradada que o painel de definições mostra, não um pedido falhado.

**200**

```json
{
  "providers": {
    "edge": {"available": true, "requires_key": false},
    "openai": {"available": false, "requires_key": true},
    "azure": {"available": false, "requires_key": true}
  },
  "default_voice": "pt-PT-RicardoMultilingualNeural",
  "default_provider": "edge",
  "voices": 53,
  "ai_gateway": {"openrouter": {"enabled": true, "...": "..."}}
}
```

A forma vem de `tts.get_tts_status()` (`tts.py:239-250`); a chave `ai_gateway` é o mesmo payload de `GET /api/providers`. Uma excepção no serviço degrada para a forma mínima com a chave `error` (`backend/app.py:367-390`).

---

## 5. `GET /api/presets`

Os 8 temas de estilo mais os vocabulários fechados que os controlos de legendas oferecem. Sem parâmetros.

**200**

```json
{
  "presets": {
    {
      "name": "cinematic", "label": "Cinematic",
      "subtitle": {"font_family": "Inter", "...": "..."},
      "palette": {"#..."},
      "transition": "fade", "effect": "cinematic"
    }
  },
  "positions": {"top", "upper", "center", "lower", "bottom"},
  "modes": {"bottom", "center", "hook", "karaoke", "hidden"},
  "fonts": {"Inter", "Montserrat", "Poppins", "..."}
}
```

Um preset tem a forma de `ThemePreset.to_dict()` (`style.py:379-387`) e o estilo a de `SubtitleStyle.to_dict()` (`style.py:272-289`). Uma falha no serviço degrada para `"presets": []` (`backend/app.py:460-472`).

---

## 6. `GET /api/music/tracks`

Biblioteca musical, opcionalmente filtrada por ambiente (query `mood`).

**200**

```json
{
  "tracks": {
    {
      "id": "ambient-drift", "title": "Ambient Drift", "mood": "ambient",
      "duration": 18.4, "path": "storage/music/ambient-drift.mp3",
      "builtin": true, "source": "builtin"
    }
  },
  "moods": {"ambient", "tension", "uplifting", "dark", "documentary", "lofi"},
  "mood": ""
}
```

São faixas sintetizadas localmente mais as carregadas pelo utilizador (`music.MOODS`, `music.py:52`; a forma da faixa é o dataclass `Track`, `music.py:65-72`).

**400** — `"Ambiente musical não suportado."` quando `mood` está presente mas não é um dos seis ambientes (`backend/app.py:478-480`).

---

## 7. `GET /api/music/search`

Pesquisa textual na biblioteca musical.

| Parâmetro | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `q` | string (query) | `""` | Termo de pesquisa. |

**200** — `{"tracks": [...], "query": "..."}` (mesma forma de faixa que `/api/music/tracks`).

**400** — `"Indique um termo de pesquisa."` quando `q` está vazio ou ausente (`backend/app.py:490-492`). Uma falha no serviço degrada para `"tracks": []` (`backend/app.py:493-496`).

---

## 8. `POST /api/music/upload`

Regista um ficheiro de áudio do utilizador na biblioteca musical. `multipart/form-data`.

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `file` | ficheiro (`File`) | *(obrigatório)* | Áudio; a extensão tem de ser `.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.flac` ou `.opus` (`music.py:50`). |
| `title` | string (`Form`) | `""` | Título da faixa. |
| `mood` | string (`Form`) | `"ambient"` | Um dos seis ambientes. |

**200**

```json
{"status": "ok", "track": {"id": "...", "title": "...", "mood": "ambient", "duration": 0.0, "path": "storage/music/....mp3", "builtin": false, "source": "upload"}}
```

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Formato de áudio não suportado para música."` | Extensão fora das sete aceite (`backend/app.py:509-512`) |
| `400` | `"Ambiente musical não suportado."` | `mood` fora dos seis ambientes (`backend/app.py:514-516`) |
| `413` | `"Ficheiro de música demasiado grande."` | Acima de 20 MB (`MAX_MUSIC_UPLOAD_BYTES`, `backend/app.py:170`; a leitura é limitada a esse valor + 1 byte) |
| `400` | `"Ficheiro de música vazio."` | Conteúdo com zero bytes (`backend/app.py:521-522`) |
| `422` | `"Não foi possível guardar o ficheiro de música."` | Falha de escrita em `storage/uploads/` (`backend/app.py:529-532`) |
| `400` | mensagem do serviço | `register_upload` recusou o título (`backend/app.py:535-537`) |
| `422` | `"Não foi possível registar a música: <detalhe>"` | Outra falha do registo (`backend/app.py:538-541`) |
| `400` | `"Nome de faixa inválido: o título não pode conter caminhos."` | Guarda contra travessia de caminhos: o caminho registado tem de ficar dentro de `storage/music/` (`backend/app.py:549-559`) |

O ficheiro é escrito sob um nome aleatório (`music_upload_<uuid>.<ext>`, `backend/app.py:526`) e apagado a seguir; o nome original nunca chega ao sistema de ficheiros.

---

## 9. `GET /api/providers`

Estado de cada fornecedor de IA/media, mais o estado de narração no mesmo payload — é o que o painel de definições chama ao carregar. Sem parâmetros.

**200**

```json
{
  "providers": {
    "gemini": {"enabled": false},
    "openai": {"enabled": false},
    "youtube": {"enabled": false},
    "pexels": {"enabled": true},
    "pixabay": {"enabled": false},
    "openrouter": {
      "enabled": true,
      "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
      "free_only": true,
      "quota_remaining": null,
      "quota_limit": null,
      "quota_exhausted": false
    }
  },
  "tts": {
    "providers": {
      "edge": {"available": true, "requires_key": false},
      "openai": {"available": false, "requires_key": true},
      "azure": {"available": false, "requires_key": true}
    },
    "default_voice": "pt-PT-RicardoMultilingualNeural",
    "default_provider": "edge",
    "voices": 53,
    "ai_gateway": {"...": "igual à chave providers"}
  }
}
```

A chave `providers` tem a forma de `provider_registry.get_provider_status()` (`provider_registry.py:700-722`): um fornecedor aparece `enabled: true` quando a chave respectiva está definida no ambiente. `quota_remaining` e `quota_limit` só ficam preenchidos depois de uma chamada real ao OpenRouter que devolva os cabeçalhos `x-ratelimit-*`; o estado vive na memória do processo e não é persistido. A chave `tts` tem a forma de `tts.get_tts_status()` (`tts.py:239-250`). Ambas as leituras são vigiadas: uma excepção degrada a chave correspondente em vez de falhar o pedido (`backend/app.py:867-889`).

---

## 10. `POST /api/generate`

<a id="post-apigenerate"></a>

Enfileira a geração completa de um tema — guião, voz, legendas, media, render — e responde imediatamente com o job. **O cliente faz *poll* a `GET /api/jobs/{job_id}`**; esta rota não espera pelo vídeo.

O corpo é JSON lido **cru**, como `Dict[str, Any]` (`backend/app.py:813`), e filtrado para os nomes de campo do dataclass `GenerationRequest` (`generator.py:125-151`) por `_generation_params` (`backend/app.py:794-809`), cujo conjunto de chaves é lido do próprio dataclass em tempo de importação (`GENERATION_PARAM_FIELDS`, `backend/app.py:789-791`). Três consequências deliberadas:

- **Uma chave desconhecida é descartada, não erra** — verificado: `{"topic": "...", "bogus_key": "x", "mood": "dark"}` devolve `202`. O motivo está no docstring: `submit_job` faz `GenerationRequest(**params)`, por isso uma chave estranha de um cliente mais antigo ou escrito à mão voltaria como `TypeError`.
- **Não é um modelo Pydantic de propósito**: um modelo duplicaria o dataclass ou transformava um tema em falta num `422` antes que a mensagem em português pudesse ser mostrada. Um corpo vazio, ou um tema em branco, devolve `400` com `"Indique um tema para gerar o video."`.
- O handler é **`async def` de propósito**: `submit_job` agenda o pipeline no *event loop* em execução (`loop.create_task`, `generator.py:440-444`); uma rota síncrona correria numa *threadpool* sem loop e o job ficaria em fila para sempre.

**Corpo** — os 18 campos do `GenerationRequest`, todos opcionais excepto `topic`:

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `topic` | string | *(obrigatório)* | Tema do vídeo. |
| `language` | string | `"pt-PT"` | Um dos cinco idiomas de `script_gen.LANGUAGES`. |
| `voice` | string | `"pt-PT-RicardoMultilingualNeural"` | Id de voz `edge-tts`. |
| `tts_provider` | string | `"edge"` | `edge`, `openai` ou `azure`. |
| `rate` | string | `"+0%"` | Velocidade da narração. |
| `section_count` | int | `5` | 1..10. |
| `tone` | string | `"documentary"` | Tom do guião. |
| `duration_target` | int | `60` | Segundos; limitado a 15..600. |
| `custom_instructions` | string | `""` | Instruções extra para o modelo. |
| `aspect_ratio` | string | `"vertical"` | `vertical`, `square` ou `landscape`. |
| `preset` | string | `"cinematic"` | Um dos 8 temas de `GET /api/presets`. |
| `subtitle_style` | object/null | `null` | Estilo de legendas (a forma de `SubtitleStyle`). |
| `music_track` | string/null | `null` | Id de faixa de `GET /api/music/tracks`; vazio deixa o pipeline escolher por `music_mood`. |
| `music_mood` | string | `"ambient"` | Ambiente para a escolha automática de música. |
| `music_volume` | float | `0.18` | 0..1. |
| `duck_voice` | bool | `true` | Sidechain da música sob a voz. |
| `include_captions` | bool | `true` | Legendas visíveis; `false` esconde-as. |
| `project_prefix` | string | `""` | Radical do nome do projecto; vazio usa o tema. |

**202** — o objecto job completo (ver [O objecto job](#o-objecto-job)):

```json
{
  "job_id": "3f9a2c1b8d4e",
  "status": "queued",
  "progress": 0.0,
  "stage": "script",
  "message": "Na fila, à espera de uma slot livre.",
  "params": {"topic": "a história do café em Portugal", "language": "pt-PT", "voice": "pt-PT-RicardoMultilingualNeural", "tts_provider": "edge", "rate": "+0%", "section_count": 5, "tone": "documentary", "duration_target": 60, "custom_instructions": "", "aspect_ratio": "vertical", "preset": "cinematic", "subtitle_style": null, "music_track": null, "music_mood": "ambient", "music_volume": 0.18, "duck_voice": true, "include_captions": true, "project_prefix": ""},
  "result": null,
  "error": null,
  "created_at": "2026-10-03T19:54:06Z",
  "updated_at": "2026-10-03T19:54:06Z",
  "_seq": 7
}
```

O nome do projecto não se envia: é derivado de `project_prefix` ou do tema por `_project_slug`/`slugify_topic` (`generator.py:82-120`), que normaliza com NFKD, decompõe os acentos e reduz tudo o que não for alfanumérico a um hífen — o resultado nunca tem separadores de caminho nem `..`.

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Indique um tema para gerar o video."` | Tema em branco (`generator.py:160`) |
| `400` | `"Idioma '<x>' não é suportado. Escolha um de pt-PT, pt-BR, en-US, es-ES, fr-FR."` | Fora dos cinco idiomas (`generator.py:164-167`) |
| `400` | `"O número de secções tem de ser um inteiro."` / `"O número de secções tem de estar entre 1 e 10."` | `section_count` (`generator.py:172-177`) |
| `400` | `"A duração-alvo tem de ser um número de segundos."` / `"A duração-alvo tem de ser maior que zero."` | `duration_target` (`generator.py:182-184`) |
| `400` | `"Proporção '<x>' inválida. Use vertical, square, landscape."` | `aspect_ratio` (`generator.py:188-191`) |
| `400` | `"Preset '<x>' inválido. Escolha um de <nomes>."` | `preset` desconhecido (`generator.py:196-199`) |
| `400` | `"O volume da música tem de ser um número."` / `"O volume da música tem de estar entre 0 e 1."` | `music_volume` (`generator.py:204-206`) |
| `500` | `"Falha ao enfileirar a geração: <detalhe>"` | Falha ao enfileirar — é um erro interno, não do chamador (`backend/app.py:832-837`) |

Todas as mensagens de `400` vêm de `GenerationRequest.validate` (`generator.py:153-229`), que valida o primeiro problema e devolve uma cópia com as omissões preenchidas. No máximo **dois** jobs correm ao mesmo tempo (`MAX_CONCURRENT_JOBS = 2`, `generator.py:45`); os restantes ficam à espera de uma *slot* e continuam a reportar `status: "queued"` — só passam a `running` quando obtêm uma das duas. É daí que vem a primeira regra de *poll*: `progress` fica em `0.0` enquanto o job espera e o primeiro valor observável é `0.05`, já dentro da fase `running`.

---

## 11. `GET /api/jobs`

Todo o histórico de jobs, **novo primeiro**. Sem parâmetros.

> **Esta é a única rota do servidor que devolve um array simples** — `[...]` em vez de `{...}` (`backend/app.py:841-844`). Um cliente que leia `resposta.jobs` obtém `undefined` em silêncio. Escreva `for (const job of await resposta.json())`. O próprio frontend se guardinga disto (`frontend/js/api.js:179-183` documenta-o explicitamente; a vista de projectos verifica `Array.isArray` antes de iterar).

**200** — `[{...objecto job...}, ...]`, ordenado por `updated_at` e `_seq`, ambos decrescentes (`generator.py:354-357`).

---

## 12. `GET /api/jobs/{job_id}`

| Parâmetro | Tipo | Descrição |
| --- | --- | --- |
| `job_id` | string (path) | Os 12 caracteres hexadecimais devolvidos pelo `202`. |

**200** — o objecto job.

**404** — `{"detail": "Job não encontrado."}` (`backend/app.py:847-852`).

---

## 13. `DELETE /api/jobs/{job_id}`

Cancela um job que ainda não começou.

**200** — `{"cancelled": true}` ou `{"cancelled": false}`.

**404** — `{"detail": "Job não encontrado."}`.

**O que `cancelled` significa.** `cancel_job` só actua quando o estado é `queued` (`generator.py:393-394`): marca o job como `cancelled`, escreve a mensagem `"Cancelado pelo utilizador antes de começar."` e o erro `"Cancelado pelo utilizador."`, e devolve `true`. Devolve `false` para um job **`running`** e para um job já terminado (`completed`, `failed` ou `cancelled`) (`generator.py:390-401`). O `404` acima é o único sinal de *job inexistente*, por isso `{"cancelled": false}` com `200` quer dizer «não cancelável», nunca «desconhecido» (`backend/app.py:855-864`).

**O cancelamento só é efectivo antes de o pipeline arrancar.** Um job em fila é mesmo cancelado: o *worker* volta a ler o registo depois de obter a *slot* e aborta se já não o encontrar `queued` (`generator.py:487-491`), pelo que nunca chega a correr uma etapa. Um job **`running`**, pelo contrário, **não se cancela** — a resposta é `{"cancelled": false}` e o trabalho continua até ao fim, com o MP4 escrito na mesma. Não é uma falha do servidor: não existe interruptor partilhado, e interromper o processo a meio do render deixaria um MP4 truncado em `storage/outputs/`. Quem quiser parar um trabalho tem de o cancelar enquanto está `queued`; a partir de `running` só resta esperar por `completed` ou `failed`. Ver [O objecto job](#o-objecto-job) e a secção de jobs e fila em [ARCHITECTURE.md](ARCHITECTURE.md#4-jobs-e-fila).

---

## 14. `POST /api/script`

Transforma um tema num guião narrado. Corpo JSON (`ScriptRequest`, `backend/app.py:140-149`).

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `topic` | string | *(obrigatório)* | Tema; não pode ser branco. |
| `project_name` | string | `""` | Quando presente, o guião é guardado em `storage/uploads/<nome>.script.json`, para `POST /api/tts` narrar sem reenviar o texto (`backend/app.py:341-345`). |
| `language` | string | `"pt-PT"` | Um dos cinco idiomas. |
| `section_count` | int | `5` | 1..10 (`MAX_SECTIONS`, `script_gen.py:41`). |
| `tone` | string | `"documentary"` | Tom do guião. |
| `duration_target` | int | `60` | Segundos; limitado a 15..600 (`script_gen.py:43-44`). |
| `custom_instructions` | string | `""` | Instruções extra para o modelo. |

A rota é **síncrona de propósito**: `generate_script` é síncrona, e envolvê-la em `async def` bloquearia o *event loop* durante a chamada ao modelo (`backend/app.py:301-306`).

**200** — o guião (`Script.to_dict()`, `script_gen.py:90-110`):

```json
{
  "title": "...",
  "hook": "...",
  "sections": {{"index": 1, "text": "...", "visual_terms": {"..."}}, ...},
  "language": "pt-PT",
  "tone": "documentary",
  "source": "openrouter-free",
  "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
  "fallback_error": null,
  "full_text": "...",
  "estimated_seconds": 24.0,
  "section_count": 5
}
```

Com `project_name`, a resposta traz ainda `"project_name": "cafe"` e `"stored": true`.

**A degradação é um sucesso.** Sem chave ou com quota esgotada, o serviço devolve o guião local de recurso com `"source": "fallback-local"`, `"fallback_error": "<motivo>"` e `"degraded": true` — e a resposta continua a ser `200` (`backend/app.py:339`). Se a sua aplicação não ler `degraded`, a degradação é invisível; para a tornar visível, `GET /api/providers` devolve `openrouter.quota_exhausted`.

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Indique um tema para gerar o guião."` | Tema branco (`backend/app.py:307-308`) |
| `400` | `"Idioma de narração não suportado."` | Fora dos cinco idiomas (`backend/app.py:309-311`) |
| `400` | `"O número de secções tem de estar entre 1 e 10."` | Fora de 1..10 (`backend/app.py:312-316`) |
| `400` | mensagem do serviço | `ValueError` de `generate_script` (`backend/app.py:328-330`) |
| `422` | `"Não foi possível gerar o guião: <detalhe>"` | Qualquer outra excepção (`backend/app.py:331-334`) |

---

## 15. `POST /api/tts`

Narra um projecto e deixa o MP3 e o SRT que `POST /api/build-video` espera. Corpo JSON (`TtsRequest`, `backend/app.py:152-160`).

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `project_name` | string | `"demo_project"` | Nome do projecto, saneado. |
| `text` | string | `""` | Texto a narrar; **vazio significa «narra o guião guardado para este projecto»** (`backend/app.py:402-404`). |
| `voice` | string | `"pt-PT-RicardoMultilingualNeural"` | Id de voz; validado antes de qualquer chamada de rede. |
| `provider` | string | `"edge"` | `edge`, `openai` ou `azure` (`tts.SUPPORTED_PROVIDERS`, `tts.py:101`). |
| `rate` | string | `"+0%"` | Velocidade. |
| `volume` | string | `"+0%"` | Volume. |
| `pitch` | string | `"+0Hz"` | Tom. |

**200**

```json
{
  "project_name": "cafe",
  "audio_file": "storage/uploads/cafe.mp3",
  "srt_file": "storage/uploads/cafe.srt",
  "segments": {{"index": 1, "start": 0.0, "end": 3.2, "text": "..."}, ...},
  "voice": "pt-PT-RicardoMultilingualNeural",
  "provider": "edge",
  "characters": 1234,
  "estimated_seconds": 98.72
}
```

`estimated_seconds` é uma estimativa a 2,5 palavras por segundo (`WORDS_PER_SECOND`, `backend/app.py:166`). A seguir a sintetizar, a rota transcreve o próprio áudio para escrever o SRT (`backend/app.py:435-438`) — e é aí que entra a [degradação silenciosa](#16-post-apitranscribe): sem `faster-whisper`, o SRT são as cinco frases fixas e a resposta continua a ser `200`.

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Fornecedor de voz não suportado."` | `provider` fora dos três (`backend/app.py:398-400`) |
| `400` | `"Não há texto para narrar: envie `text` ou gere um guião primeiro."` | Sem `text` e sem guião guardado (`backend/app.py:406-409`) |
| `400` | mensagem do serviço | Voz inválida ou texto vazio — `ValueError` antes de qualquer chamada (`backend/app.py:426-428`) |
| `401`/`403`/`402`/`429`/`502` | contrato `AUTH_*` | Chave em falta, rejeitada, quota, limite ou falha de rede do fornecedor — `AuthError` re-levantada para o handler global (`backend/app.py:422-425`) |
| `422` | `"Não foi possível gerar a narração: <detalhe>"` | `edge-tts` ou `ffmpeg` em falta — `RuntimeError` (`backend/app.py:429-433`) |
| `422` | `"Não foi possível gerar as legendas: <detalhe>"` | A transcrição do áudio falhou (`backend/app.py:439-446`) |

---

## 16. `POST /api/transcribe`

Recebe um áudio, transcreve-o e escreve `storage/uploads/<nome>.srt`. `multipart/form-data`.

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `file` | ficheiro (`File`) | *(obrigatório)* | Áudio de origem; sem validação de formato nem limite de tamanho. |
| `project_name` | string (`Form`) | `"demo_project"` | Nome do projecto, saneado. |

O conteúdo do upload é gravado tal e qual, com a extensão deduzida do nome do ficheiro (`.mp3` se não houver ponto) (`backend/app.py:568-572`).

**200**

```json
{
  "project_name": "meu_projeto",
  "audio_file": "storage/uploads/meu_projeto.mp3",
  "srt_file": "storage/uploads/meu_projeto.srt",
  "segments": {{"index": 1, "start": 0.0, "end": 3.2, "text": "..."}, ...}
}
```

> **Degradação silenciosa — o ponto mais fácil de não ver.** `pipeline.transcribe_audio_file` (`pipeline.py:64`) corre o `faster-whisper` dentro de um `try/except` que engole **qualquer** excepção. Duas condições diferentes caem no mesmo caminho: o pacote em falta (`ImportError`) **e** o modelo a devolver zero segmentos. As duas resultam nas mesmas cinco frases fixas em português, cada uma de 3 segundos (`pipeline.py:84-94`). O endpoint responde `200`, o SRT fica errado e **não existe campo de aviso**. A única forma de distinguir é comparar o texto devolvido com o áudio. Um `.srt` inventado é exactamente o que `/api/tts` e `/api/build-video` vão consumir a seguir. Verificado: um MP3 de 3 bytes produz `200` com o SRT de recurso.

---

## 17. `POST /api/build-video`

Pipeline manual completo: SRT -> palavras-chave -> storyboard -> pesquisa de media por cena -> plano de edição -> composição HTML -> render HyperFrames -> mistura de música. `multipart/form-data`; todos os campos são strings.

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `project_name` | string (`Form`) | `"demo_project"` | **Requer** `storage/uploads/<nome>.srt`. |
| `transition` | string (`Form`) | `"fade"` | Transição aplicada a todas as cenas. |
| `effect` | string (`Form`) | `"cinematic"` | Efeito de movimento por cena. |
| `include_captions` | string (`Form`) | `"true"` | `"true"` = legendas visíveis; qualquer outro valor = `hidden`. |
| `storyboard_json` | string (`Form`) | `""` | Storyboard personalizado: JSON com uma **lista** de objectos. |
| `aspect_ratio` | string (`Form`) | `"vertical"` | `vertical` 1080x1920, `square` 1080x1080, `landscape` 1920x1080 (`render_engine.get_dimensions`, `render_engine.py:218`); outro valor cai em `vertical`. |
| `topic` | string (`Form`) | `""` | Nicho, usado como contexto para os termos visuais por IA. |
| `preset` | string (`Form`) | `""` | Tema de estilo (ver `GET /api/presets`). |
| `subtitle_style` | string (`Form`) | `""` | JSON com um estilo de legendas (a forma de `SubtitleStyle`). |
| `music_track` | string (`Form`) | `""` | Id de faixa (ver `GET /api/music/tracks`). |
| `music_volume` | string (`Form`) | `""` | Número de 0 a 1; por omissão `0.18` (`music.DEFAULT_MUSIC_VOLUME`, `music.py:55`). |
| `duck_voice` | string (`Form`) | `"true"` | Sidechain da música sob a voz. |

**200**

```json
{
  "project_name": "meu_projeto",
  "keywords": {"economia", "papel moeda"},
  "media": {{"source": "Pexels", "provider": "pexels", "title": "economia", "url": "https://images.pexels.com/...", "kind": "image", "width": 1880, "height": 1253}, ...},
  "scene_media": {"0": {}},
  "segments": {},
  "storyboard": {},
  "edit_plan": {},
  "scene_count": 5,
  "scenes_with_media": 4,
  "term_source": "ai",
  "provider_status": {"openrouter": {"enabled": true, "...": "..."}},
  "render": {
    "project_name": "meu_projeto", "output_stem": "meu_projeto",
    "status": "rendered",
    "srt_path": "storage/uploads/meu_projeto.srt",
    "output_path": "storage/outputs/meu_projeto.mp4",
    "storyboard": {}, "scene_count": 5, "scenes_with_media": 4,
    "rejected_assets": {}, "aspect_ratio": "vertical",
    "resolution": "1080x1920", "audio_duration": 42.1, "video_duration": 42.1,
    "message": "Vídeo renderizado com HyperFrames com sucesso.",
    "render_engine": "hyperframes", "preset": "cinematic",
    "subtitle_style": {"font_family": "Inter", "...": "..."},
    "music": {"requested": null, "applied": false, "track_id": null, "volume": 0.18, "duck_voice": true, "note": "musica nao aplicada: ..."}
  },
  "render_real": {"...": "o mesmo objecto que render"},
  "preset": "cinematic",
  "subtitle_style": null,
  "music_track": "",
  "music": null,
  "music_volume": 0.18,
  "duck_voice": true
}
```

`render` e `render_real` são **o mesmo objecto**, não uma cópia. O payload de render vem de `render_engine.render_video_hyperframes` (`render_engine.py:1085-1104`); `preset`, `subtitle_style`, `music_track`, `music` e `music_volume` são ecoados para a interface mostrar o que foi efectivamente aplicado (`backend/app.py:705-726`). A escolha de media é explícita: o pool por cena ganha, e o pool global só é usado quando não há nenhum (`backend/app.py:655-657`).

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Gere primeiro a transcrição deste projeto."` | Falta o `.srt` deste projecto (`backend/app.py:602-605`) |
| `400` | `"Estilo de legendas inválido: JSON malformado."` | `subtitle_style` não é JSON (`backend/app.py:611-616`) |
| `400` | `"Estilo de legendas tem de ser um objecto JSON."` | `subtitle_style` não é um objecto (`backend/app.py:617-620`) |
| `400` | `"O volume da música tem de ser um número entre 0 e 1."` | `music_volume` não é numérico ou fora de 0..1 (`backend/app.py:622-626`) |
| `400` | `"Storyboard inválido."` | `storyboard_json` não é JSON (`backend/app.py:643-645`) |
| `400` | `"Storyboard deve ser uma lista de cenas."` | Não é uma lista (`backend/app.py:646-647`) |
| `400` | `"Cada cena do storyboard deve ser um objecto."` | Contém algo que não seja um objecto (`backend/app.py:648-649`) |

O render em si **nunca** devolve erro HTTP: uma falha de `ffmpeg` ou de `node` chega num payload com `"status": "error"` e as chaves `error`/`message` (`render_engine.py:1105-1122`). A música é pós-processo e nunca fatal: uma faixa em falta ou um `ffmpeg` em falta saem em `render.music.applied` falso, com uma nota a explicar porquê.

---

## 18. `POST /api/build-viral`

Pipeline viral: detecção de batidas com `librosa`, cortes alinhados ao ritmo, loop contínuo, thumbnail HTML e metadados de plataforma. `multipart/form-data`.

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `project_name` | string (`Form`) | `"viral_project"` | Requer `storage/uploads/<nome>.srt`. |
| `aspect_ratio` | string (`Form`) | `"vertical"` | `vertical`, `square`, `landscape`. |
| `include_karaoke` | string (`Form`) | `"true"` | `"false"` desliga o karaoke na primeira cena. |
| `topic` | string (`Form`) | `""` | Tema, para os metadados e a categoria. |
| `niche` | string (`Form`) | `""` | Nicho, para pesquisa de media e categoria. |

**200**

```json
{
  "project_name": "meu_projeto",
  "status": "rendered",
  "output_path": "storage/outputs/meu_projeto_viral.mp4",
  "video_duration": 42.1,
  "resolution": "1080x1920",
  "aspect_ratio": "vertical",
  "beat_data": {"tempo": 96.0, "beats": {}, "beat_count": 68, "duration": 42.1},
  "platform_metadata": {
    "hashtags": {"#economia", "#viral"},
    "category": "Education", "title_options": {},
    "description": "...", "sound_suggestions": {},
    "posting_times": {}
  },
  "thumbnail_path": "storage/thumbnails/meu_projeto_thumb.html",
  "loop_duration": 0.5,
  "hook_duration": 3.0,
  "scenes_with_media": 6,
  "storyboard": {},
  "render_engine": "hyperframes",
  "message": "Vídeo viral pronto: beat-sync, loop contínuo, thumbnail otimizado, metadados de plataforma."
}
```

O MP4 viral fica em `storage/outputs/<projeto>_viral.mp4` e os metadados em `storage/outputs/<projeto>_viral_meta.json`. O `thumbnail_path` aponta para um **HTML**, não para um PNG: é um documento para compor, não uma imagem já renderizada.

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Gere primeiro a transcrição deste projeto."` | Falta o `.srt` (`backend/app.py:737-740`) |
| `503` | `"O pipeline viral precisa do pacote opcional 'librosa'. Instale-o com 'pip install -r requirements.txt'."` | `librosa` não instalado (`backend/app.py:747-753`) |
| `422` | mensagem do pipeline | O pipeline devolveu `"status": "error"` (`backend/app.py:762-763`) |

> **O import do `librosa` é preguiçoso — o servidor arranca sem ele.** `viral_pipeline` é importado **dentro** do corpo de `build_viral` (`backend/app.py:745`), precisamente para o servidor não depender do `librosa`. Isto já foi diferente: um import no topo do módulo fazia o servidor inteiro falhar de arranque sem o pacote. Hoje, sem `librosa`, **só esta rota** falha — com `503` e a mensagem acima, que diz como o instalar. Ver [INSTALL.md](INSTALL.md#14-dependências-python).

---

## 19. `POST /api/shorts`

Corte vertical com legendas karaoke a partir de um projecto transcrito. `multipart/form-data`.

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `project_name` | string (`Form`) | `"demo_project"` | Requer `storage/uploads/<nome>.srt`. |
| `aspect_ratio` | string (`Form`) | `"vertical"` | `vertical`, `square`, `landscape`; qualquer outro valor é normalizado para `vertical`. |
| `include_karaoke` | string (`Form`) | `"true"` | `"false"` remove as palavras e volta a legendas simples. |
| `max_highlights` | int (`Form`) | `3` | Limitado a 1..5 (`backend/app.py:1038`). |

**200**

```json
{
  "project_name": "meu_projeto",
  "status": "rendered",
  "output_stem": "meu_projeto_shorts",
  "output_path": "storage/outputs/meu_projeto_shorts.mp4",
  "highlights_count": 3,
  "scenes": {},
  "aspect_ratio": "vertical",
  "planned_duration": 12.7,
  "video_duration": 12.7,
  "resolution": "1080x1920",
  "has_audio": false,
  "audio_note": "corte sem audio: as palavras ja estao nas legendas",
  "karaoke": true,
  "message": "Short pronto: 3 destaques, 12.7s",
  "render_engine": "hyperframes"
}
```

O resumo tem a forma de `generate_shorts` (`shorts_pipeline.py:447-466`) e é também gravado em `storage/outputs/<nome>_shorts.json` (`shorts_pipeline.py:470-472`). O corte **não leva a faixa de áudio original** (`"has_audio": false`): os destaques vêm de pontos espalhados da narração e as palavras já estão queimadas nas legendas, por isso a faixa tocaria por baixo de legendas que não correspondem.

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `404` | `"SRT file not found. Transcribe the project first."` | `SHORTS_NO_SRT` — falta o `.srt` (`shorts_pipeline.py:385-392`) |
| `404` | `"No segments found in the SRT file."` | `SHORTS_NO_SEGMENTS` — SRT vazio (`shorts_pipeline.py:394-401`) |
| `422` | `"No highlight long enough for a short was found."` / `"Highlights produced no renderable scenes."` | `SHORTS_NO_HIGHLIGHTS` (`shorts_pipeline.py:403-426`) |
| `422` | mensagem do render | `"status": "error"` do render (`backend/app.py:1045-1047`) |

---

## 20. `GET /api/shorts/{project_name}/highlights`

Destaques de um projecto, **sem** renderizar. Usa o mesmo `detect_highlights` do `/api/shorts`.

**200** com SRT

```json
{
  "project_name": "meu_projeto",
  "status": "ok",
  "highlights_count": 3,
  "highlights": {{"start": 5.2, "end": 8.5, "text": "...", "importance": 0.9}, ...},
  "total_seconds": 12.7
}
```

**200** sem SRT — `{"project_name": "...", "status": "error", "error": "SRT file not found."}`. Nesta rota a ausência de SRT **não** é `404`.

**400** — `"Nome de projeto inválido."` quando o nome fica vazio após o saneamento (`backend/app.py:1022-1027`). A forma completa vem de `list_highlights` (`shorts_pipeline.py:476-492`).

---

## 21. `POST /api/strategy`

Ângulo editorial para um nicho. Corpo JSON (`StrategyRequest`, `backend/app.py:135-137`).

| Campo | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `niche` | string | *(obrigatório)* | Tema ou nicho; não pode ser branco. |
| `transcript` | string | `""` | Texto disponível; truncado a 4000 caracteres no prompt (`backend/app.py:902`). |

**200** — resposta do modelo:

```json
{
  "angle": "...",
  "hook": "...",
  "titles": {"...", "...", "..."},
  "visual_direction": "...",
  "chapters": {"...", "...", "..."},
  "source": "openrouter-free",
  "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
}
```

**200** — fallback local: a mesma forma, com `"source": "fallback-local"`, `"fallback_error": "<motivo>"` e conteúdo fixo em português derivado do nicho.

Indisponibilidade do modelo **não** é erro HTTP: o fallback local é um sucesso, e `source` diz sempre qual dos dois respondeu — o fallback nunca esconde o motivo (`backend/app.py:919-932`). A resposta é validada antes de ser devolvida: `titles` e `chapters` têm de ser arrays com pelo menos três itens, ou o pedido cai no fallback. Só `niche` branco devolve `400` `"Indique um tema ou nicho."` (`backend/app.py:894-896`).

---

## 22. `POST /api/settings`

Grava as chaves em `.env` na raiz do projecto e actualiza `os.environ` no processo corrente. Corpo JSON (`ApiSettings`, `backend/app.py:123-132`) — nove campos, todos `string`:

| Campo | Predefinição |
| --- | --- |
| `openrouter_api_key` | `""` |
| `openrouter_model` | `"meta-llama/llama-3.3-8b-instruct:free"` |
| `pexels_api_key` | `""` |
| `pixabay_api_key` | `""` |
| `gemini_api_key` | `""` |
| `openai_api_key` | `""` |
| `youtube_api_key` | `""` |
| `azure_speech_key` | `""` |
| `azure_speech_region` | `""` |

Regras:

- `openrouter_model` que **não** termine em `:free` devolve `400` `"Use apenas um modelo OpenRouter terminado em :free."` (`backend/app.py:937-938`). É uma invariante do projecto, não um erro ocasional: a recusa é repetida antes de cada chamada ao OpenRouter (`provider_registry.py:751-757`), e não existe parâmetro nenhum que permita pedir um modelo pago.
- `openrouter_model` vazio assume `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (`backend/app.py:941`).
- O `.env` é **reescrito por inteiro** com exactamente estas nove chaves (`backend/app.py:952-953`): uma linha que não seja uma delas desaparece quando a vista de definições grava. As chaves nunca são devolvidas pela API.
- No `os.environ`, um valor vazio **remove** a variável; para `GEMINI_API_KEY`, `OPENAI_API_KEY`, `YOUTUBE_API_KEY`, `AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION`, um envio vazio cai primeiro para o valor que já existe no ambiente, por isso não apaga nada (`backend/app.py:944-950`). A Azure precisa das duas metades em conjunto — são gravadas lado a lado precisamente para uma chave guardada nunca ficar sem região.

**200** — `{"status": "saved", "providers": {..."a mesma forma de GET /api/providers"...}}`.

**Erros:** `400` para modelo sem `:free`.

---

## 23. `POST /api/ai/test`

Dispara uma chamada real ao modelo `:free` configurado («Responda apenas: OK»). Sem corpo.

**200** — `{"status": "ok", "provider": "openrouter", "model": "...:free", "response": "OK"}` (a resposta é truncada a 80 caracteres).

**Erros**

| Código | Corpo | Quando |
| --- | --- | --- |
| `401`/`403`/`402`/`429`/`502` | `{"status": "error", "error_code": "AUTH_...", "message": "...", "details": {...}}` | Contrato `AUTH_*` — a rota devolve-o com o estado do contrato em vez de o achatar (`backend/app.py:972-974`) |
| `200` | `{"status": "error", "provider": "openrouter", "model": "...", "error": "..."}` | Erro não-auth — rede, `RuntimeError`, `ValueError` (`backend/app.py:975-981`) |

---

## 24. `GET /api/media/search`

Pesquisa directa num fornecedor de stock media.

| Parâmetro | Tipo | Predefinição | Descrição |
| --- | --- | --- | --- |
| `query` | string (query) | *(obrigatório)* | Termo de pesquisa; ausente devolve `422` do FastAPI. |
| `provider` | string (query) | `"pexels"` | `pexels` ou `pixabay`. |

**200**

```json
{
  "query": "economia",
  "provider": "pexels",
  "results": {
    {"source": "Pexels", "provider": "pexels", "title": "economia", "url": "https://images.pexels.com/...", "kind": "image", "width": 1880, "height": 1253}
  }
}
```

Sem fornecedor configurado, com pesquisa em branco ou com um erro não-HTTP do fornecedor, a resposta é `{"results": {}}` (`pipeline.py:593-598`, `pipeline.py:660-665`). Uma chave rejeitada (401/402/403/429) ou um 5xx do fornecedor **não** se converte em lista vazia: propaga o contrato `AUTH_*`, para que uma credencial partida não pareça «esta pesquisa não tem resultados» (`pipeline.py:653-659`).

---

## 25. `GET /api/projects`

Lista **ficheiros** de `storage/uploads/` — **não** projectos. **Não existe base de dados de projectos**; um projecto é, operacionalmente, «os ficheiros que partilham um mesmo radical», e é o frontend que os agrupa por nome (`frontend/js/projects.js:13`). Sem parâmetros.

**200** — `{"projects": {{"name": "meu_projeto.srt", "path": "storage/uploads/meu_projeto.srt"}, {"name": "meu_projeto.mp3", "path": "storage/uploads/meu_projeto.mp3"}}}`

O `.srt` e o `.mp3` do mesmo projecto aparecem como duas entradas. A lista é ordenada e lida do directório a cada chamada (`backend/app.py:1013-1019`).

---

## 26. `GET /api/project/{project_name}/video`

Descarrega o MP4 principal de um projecto (`storage/outputs/<nome>.mp4`).

**200** — `video/mp4`, stream de ficheiro (não JSON).

**Erros**

| Código | `detail` | Quando |
| --- | --- | --- |
| `400` | `"Nome de projeto inválido."` | Nome que fica vazio após o saneamento (`backend/app.py:991-993`) |
| `404` | `"Vídeo ainda não foi gerado."` | O MP4 não existe (`backend/app.py:995-998`) |

O `404` é `404` e não `200`-com-JSON **de propósito**: um elemento `<video>` não consegue ler um corpo de erro e falharia em silêncio.

---

## 27. `GET /api/project/{project_name}/shorts/video`

Igual ao anterior, para `storage/outputs/<nome>_shorts.mp4`.

**200** — `video/mp4`. **Erros:** `400` `"Nome de projeto inválido."`; `404` `"Short ainda não foi gerado."` (`backend/app.py:1002-1010`).

---

## O objecto job

O que `POST /api/generate` devolve e o que `GET /api/jobs*` lê. `Job.to_dict()` (`generator.py:261-276`):

| Campo | Tipo | Descrição |
| --- | --- | --- |
| `job_id` | string | 12 caracteres hexadecimais (`uuid4().hex[:12]`, `generator.py:423`). |
| `status` | string | `queued`, `running`, `completed`, `failed` ou `cancelled`. Um job à espera de *slot* é `queued`; só passa a `running` quando a obtém — ver abaixo. |
| `progress` | float | 0.0..1.0. |
| `stage` | string | `script`, `voice`, `media`, `render` ou `done`. |
| `message` | string | Mensagem em português do marco actual. |
| `params` | object | O `GenerationRequest` validado — os 18 campos da tabela de `POST /api/generate`. |
| `result` | object/null | O payload final, só preenchido em `completed`. |
| `error` | string/null | Mensagem de falha em português. |
| `created_at` / `updated_at` | string | ISO-8601 UTC sem offset (`2026-10-03T19:54:06Z`), granularidade de **segundos**. |
| `_seq` | int | Contador monotónico de inserção. |

`_seq` existe porque os timestamps ISO-8601 só têm granularidade de segundo: dois jobs submetidos no mesmo segundo empatariam em `updated_at`, e o contador é o desempate estável que mantém `GET /api/jobs` novo-primeiro mesmo quando o relógio não avançou (`generator.py:255-259`, ordenação em `generator.py:354-357`).

**O ciclo de vida observável é `queued` -> `running` -> `completed` | `failed`, mais `cancelled` para um job ainda em fila.** A mudança para `running` acontece no *worker*, no instante a seguir a obter uma das *slots* do semáforo: `_running_ids.add(job_id)` e `job.status = "running"`, em duas linhas seguidas (`generator.py:492-493`), com a primeira marca de etapa logo a seguir (`generator.py:494`). `_stage` continua a mexer só em `stage`, `progress` e `message` — nunca em `status` (`generator.py:550-555`) — por isso é o `status` que diz se o job está à espera ou a trabalhar, e são `stage`, `progress` e `message` que dizem **em que**.

Um cliente distingue as duas fases só por `status`: `queued` significa «à espera de uma *slot* livre» e vem sempre com `progress: 0.0` e `stage: "script"`; `running` significa «tem uma das duas *slots* e está a trabalhar», e é a única fase em que `progress` avança. É também o `status` que decide o cancelamento — ver [DELETE /api/jobs/{job_id}](#13-delete-apijobsjob_id).

O painel espelha a mesma distinção: `queued` mapeia para «inactivo» antes de olhar para a etapa (`frontend/js/generator.js:39`), enquanto `running` mapeia para a etapa real do pipeline (`frontend/js/generator.js:40-47`), com o rótulo de estado `"A trabalhar"` (`frontend/js/generator.js:30`) e a marca `"a correr"` ao lado da etapa activa (`frontend/js/generator.js:96`).

Escada de etapas e progresso (`generator.py:53-57`, `generator.py:595-713`):

| Etapa | Progresso | Mensagem | O que acontece |
| --- | --- | --- | --- |
| `script` | `0.05` | `"A gerar o guião…"` | `script_gen.generate_script` — modelo `:free` ou guião local de recurso |
| `voice` | `0.20` | `"A sintetizar a narração…"` | `tts.synthesize_speech_long`, `pipeline.transcribe_audio_file` e escrita do SRT |
| `media` | `0.40` | `"A buscar imagens de stock…"` | Storyboard e `pipeline.search_media_for_scenes` — uma pesquisa por cena |
| `render` | `0.60` | `"A renderizar com HyperFrames…"` | `style.resolve_preset_and_style`, `music.pick_track`, `render_engine.render_video_hyperframes` |
| `done` | `1.00` | `"Vídeo pronto."` | Resultado anexado ao job; `status` passa a `completed` e `message` a `"Vídeo gerado com sucesso."` |

Os valores vivem em constantes nomeadas porque são contrato público: quem os consulta não deve adivinhar. Toda a escada corre dentro da fase `running`: um job à espera de *slot* fica em `progress: 0.0` com `stage: "script"` e a mensagem de fila, e só vê `0.05` para `script` depois de obter a *slot* (`generator.py:595`).

**Degradação, nunca falha.** Qualquer falha de etapa é apanhada, o job passa a `status: "failed"` com mensagem em português e `progress: 0.0`, e a excepção é engolida — um job falhado nunca bloqueia a fila nem propaga para quem o chamou (`generator.py:558-566`, com uma segunda rede no *worker* em `generator.py:495-521`). Um `AuthError` é desembrulhado para `CODIGO: mensagem (HTTP n)`, para o código sobreviver à travessia do pipeline em vez de ficar perdido dentro de um prefixo genérico (`generator.py:506-510`).

**Concorrência e poda.** No máximo **dois** jobs correm ao mesmo tempo (`MAX_CONCURRENT_JOBS = 2`, `generator.py:45`) — cinco renders simultâneos esgotariam a memória de uma máquina normal; o semáforo é criado ao nível do módulo e reutilizado entre *event loops*, porque o `pytest-asyncio` cria um por teste. Jobs terminados com mais de **uma hora** são podados a cada mutação, com um tecto duro de **200** entradas (`MAX_JOB_HISTORY`, `generator.py:49`; `_prune_locked`, `generator.py:310-333`); jobs em fila ou em trabalho **nunca** são podados, ou o servidor perderia trabalho. As leituras públicas devolvem cópias profundas — um cliente não consegue mexer no registo pelo que recebeu (`generator.py:346-349`, `generator.py:362-378`).

---

## Contrato `AUTH_*`

Falhas de fornecedor — de IA ou de media — chegam ao cliente com a mesma forma, definida em `backend/services/auth_contract.py`. Um handler de excepção instalado em `backend/app.py:81-84` transforma `AuthError` na resposta, por isso o corpo e o código de estado viajam sempre juntos:

```json
{
  "error_code": "AUTH_RATE_LIMIT",
  "message": "Rate limit atingido no fornecedor livre. Tente novamente em instantes.",
  "details": {"provider": "openrouter", "upstream_status": 429}
}
```

**Seis códigos, emitidos pelo servidor** (`auth_contract.py:6-11`):

| `error_code` | HTTP | Quando | Detalhes típicos |
| --- | --- | --- | --- |
| `AUTH_MISSING_KEY` | `401` | Chave não configurada | `provider` |
| `AUTH_INVALID_KEY` | `401` ou `403` | Chave rejeitada; **preserva o estado devolvido pelo fornecedor** | `provider`, `upstream_status`, às vezes `upstream_body` |
| `AUTH_RATE_LIMIT` | `429` | Limite de pedidos | `provider`, `upstream_status`, `upstream_body` |
| `AUTH_MODEL_NOT_FREE` | `400` | Modelo sem `:free` | `model`, `provider` |
| `AUTH_QUOTA_EXCEEDED` | `402` | Créditos ou **quota diária de modelos `:free`** esgotados | `provider`, `upstream_status`, `upstream_body` |
| `AUTH_REQUEST_FAILED` | `502` (ou o estado do fornecedor, quando não é 5xx) | Falha de rede, resposta vazia, outro estado do fornecedor | `provider`, `exception` ou `upstream_status` |

O mapeamento estado-a-código é `provider_status_error` (`auth_contract.py:91-134`): 401/403 -> `AUTH_INVALID_KEY`, 402 -> `AUTH_QUOTA_EXCEEDED`, 429 -> `AUTH_RATE_LIMIT`, resto -> `AUTH_REQUEST_FAILED` com `502` quando o estado é 5xx e o próprio estado caso contrário. `AuthError` herda de `RuntimeError` (`auth_contract.py:14-32`), pelo que um `except RuntimeError` já existente continua a apanhar tudo — mas `error_code` e `status_code` ficam disponíveis para a camada de API.

**Códigos que o servidor nunca emite.** O `frontend/auth-handler.js` sintetiza mais **quatro** códigos do lado do navegador — `AUTH_SERVER_ERROR` (estado HTTP >= 500 sem contrato), `AUTH_NETWORK_ERROR` (falha de ligação), `AUTH_TIMEOUT` (pedido abortado após 30 s) e `AUTH_UNKNOWN` (último recurso) (`auth-handler.js:60-75`, `auth-handler.js:252-265`). São códigos **do navegador**: nenhum deles aparece num `error_code` devolvido pelo servidor. A distinção importa a quem depura: um `AUTH_TIMEOUT` num toast nunca veio do backend.

| Código | Origem | Mensagem (pt-PT) | Comportamento |
| --- | --- | --- | --- |
| `AUTH_MISSING_KEY` | servidor | `"Chave de API em falta. Adicione-a nas configurações para continuar."` | Redirecciona para `/#settings` |
| `AUTH_INVALID_KEY` | servidor | `"Chave de API rejeitada. Verifique a chave nas configurações."` | Redirecciona para `/#settings` |
| `AUTH_RATE_LIMIT` | servidor | `"Limite de pedidos atingido. A tentar novamente dentro de momentos."` | Reenvio automático com backoff |
| `AUTH_QUOTA_EXCEEDED` | servidor | `"Cota da API esgotada. Actualize o plano ou amanhã tente novamente."` | Sem reenvio automático |
| `AUTH_SERVER_ERROR` | **navegador** | `"O fornecedor de IA está com problemas. Tente novamente mais tarde."` | Reenvio automático |
| `AUTH_NETWORK_ERROR` | **navegador** | `"Sem ligação ao servidor. Verifique a sua rede e tente novamente."` | Reenvio automático |
| `AUTH_TIMEOUT` | **navegador** | `"O pedido demorou demasiado. Verifique a ligação e tente novamente."` | Reenvio automático |
| `AUTH_UNKNOWN` | **navegador** | `"Ocorreu um erro inesperado. Tente novamente ou contacte o suporte."` | Sem reenvio automático |

`RETRYABLE_CODES` = `AUTH_RATE_LIMIT`, `AUTH_SERVER_ERROR`, `AUTH_NETWORK_ERROR`, `AUTH_TIMEOUT` (`auth-handler.js:41`). `SETTINGS_REDIRECT_CODES` = `AUTH_MISSING_KEY`, `AUTH_INVALID_KEY` (`auth-handler.js:43`). O reenvio vai até 3 tentativas, com atraso de 1 s a 15 s e *timeout* de 30 s (`auth-handler.js:45-54`, `auth-handler.js:208-279`). Feedback por omissão: `toast`; `banner`, `modal` e `silent` também são aceites. Um erro do backend que **não** traga `error_code` cai em `AUTH_UNKNOWN`.

---

## Porta e origens

| Variável | Onde é lida | Efeito real |
| --- | --- | --- |
| `DARK_STUDIO_PORT` | **não é lida por `backend/app.py`** | Os atalhos escolhem a porta: base `8013`, depois a primeira livre até `DARK_STUDIO_PORT_MAX` (`start.bat:111-116`, `start.sh:65`). Definir a variável sem passar pelo atalho não muda a porta do servidor. |
| `DARK_STUDIO_ALLOWED_ORIGINS` | `backend/app.py:92`, lida em `backend/app.py:97-99` | Lista de origens CORS separada por vírgulas; sem a variável, o par histórico `http://127.0.0.1:8013` e `http://localhost:8013` (`backend/app.py:90`). |

O middleware CORS (`backend/app.py:102-108`) permite:

- **`allow_origins`**: a lista acima.
- **`allow_origin_regex`**: `^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$` (`backend/app.py:91`) — **qualquer** `localhost`, `127.0.0.1` ou `[::1]` em **qualquer** porta, sem configuração. A regex existe porque o arranque escolhe a porta livre: uma lista fixa rejeitaria a interface em qualquer máquina cuja porta mudou.
- `allow_methods` e `allow_headers`: `*`.

Consequências práticas:

- A interface funciona em qualquer porta local, incluindo `http://[::1]:<porta>` — nada a configurar.
- Em acesso remoto, a origem do cliente tem de aparecer em `DARK_STUDIO_ALLOWED_ORIGINS`, com esquema e porta, exactamente igual à que o navegador mostra na barra de endereços. `localhost:8013` sem esquema não funciona.
- O servidor nativo faz *bind* a `127.0.0.1`, não a `0.0.0.0` — só a máquina local acede. No Docker, o *bind* é `0.0.0.0` dentro do contentor.

---

## Documentação automática do FastAPI

`/docs`, `/redoc` e `/openapi.json` existem por defeito. Descrevem correctamente os modelos Pydantic das rotas que os usam (`ScriptRequest`, `TtsRequest`, `StrategyRequest`, `ApiSettings`), mas **não** descrevem `/api/generate` correctamente — o corpo é um `Dict[str, Any]` cru, e o contrato real é o dataclass `GenerationRequest` (`generator.py:125-151`). Também não mostram que `GET /api/jobs` devolve um array simples. Use esta referência para as rotas de geração.

---

## Exemplos

Fluxo de um clique:

```bash
curl -s -X POST http://127.0.0.1:8013/api/generate \
     -H 'Content-Type: application/json' \
     -d '{"topic":"a história do café em Portugal","section_count":5}'
curl -s http://127.0.0.1:8013/api/jobs/<job_id>
```

Fluxo manual:

```bash
curl -s -X POST http://127.0.0.1:8013/api/script \
     -H 'Content-Type: application/json' \
     -d '{"topic":"a história do café em Portugal","project_name":"cafe"}'
curl -s -X POST http://127.0.0.1:8013/api/tts \
     -H 'Content-Type: application/json' \
     -d '{"project_name":"cafe"}'
curl -s -X POST http://127.0.0.1:8013/api/build-video \
     -F project_name=cafe -F aspect_ratio=vertical
curl -o cafe.mp4 http://127.0.0.1:8013/api/project/cafe/video
```

Catálogos (nunca falham: devolvem listas vazias em vez de erro):

```bash
curl -s http://127.0.0.1:8013/api/presets       # 8 temas + vocabulários de legendas
curl -s http://127.0.0.1:8013/api/voices        # 53 vozes edge-tts
curl -s http://127.0.0.1:8013/api/languages     # pt-PT, pt-BR, en-US, es-ES, fr-FR
curl -s http://127.0.0.1:8013/api/music/tracks  # biblioteca musical + 6 ambientes
```

Chaves de API, como faz a vista de definições (escreve o `.env` no disco):

```bash
curl -s -X POST http://127.0.0.1:8013/api/settings \
     -H 'Content-Type: application/json' \
     -d '{"openrouter_api_key":"...","openrouter_model":"meta-llama/llama-3.3-8b-instruct:free"}'
```

Um corte curto e um viral:

```bash
curl -s -X POST http://127.0.0.1:8013/api/shorts -F project_name=cafe -F max_highlights=3
curl -s -X POST http://127.0.0.1:8013/api/build-viral -F project_name=cafe -F topic=economia
```
