# Arquitectura

Mapa de módulos com contagens de linhas reais, o pipeline etapa a etapa, o modelo de jobs e fila, o caminho do render, o grafo de módulos do frontend e o layout de dados. Para a instalação ver [INSTALL.md](INSTALL.md); para as rotas ver [API.md](API.md).

Todas as contagens vêm de `wc -l` sobre a árvore actual.

---

## 1. Mapa de módulos

### Backend — 9889 linhas

| Módulo | Linhas | Responsabilidade |
| --- | --- | --- |
| `backend/app.py` | 1048 | 27 rotas FastAPI, CORS, montagem de `/app`, modelos Pydantic, saneamento de nomes |
| `backend/services/render_engine.py` | 1418 | Composição HTML, HyperFrames via `npx`, mistura de música com `ffmpeg`, directório de projecto |
| `backend/services/provider_registry.py` | 1027 | Registo de fornecedores de IA, chaves múltiplas, memória de quota, retentativas |
| `backend/services/music.py` | 798 | Biblioteca musical: 6 faixas sintetizadas, uploads, manifesto, loop e parâmetros de mistura |
| `backend/services/pipeline.py` | 1103 | Transcrição, SRT a partir de texto, palavras-chave, storyboard, pesquisa de media, cache |
| `backend/services/generator.py` | 1185 | Orquestrador de um clique, registo de jobs, fila, marcos de progresso, legendas e relatório de duração |
| `backend/services/style.py` | 626 | 8 temas, `SubtitleStyle`, validação e vocabulários fechados |
| `backend/services/tts.py` | 951 | Catálogo de vozes `edge-tts` (41 no *snapshot* datado, ou o catálogo ao vivo), OpenAI e Azure; divisão de texto longo; estado de disponibilidade |
| `backend/services/script_gen.py` | 575 | Guião em 5 idiomas, recurso local, extracção de termos visuais |
| `backend/services/shorts_pipeline.py` | 492 | Detecção de destaques, karaoke por palavra, cut de formato curto |
| `backend/services/viral_pipeline.py` | 364 | Beat-sync, loop contínuo, thumbnail, metadados de plataforma |
| `backend/services/asset_quality.py` | 158 | Rejeição de imagens com moldura branca, via `ffmpeg signalstats` |
| `backend/services/auth_contract.py` | 143 | Seis códigos `AUTH_*`, `AuthError`, mapeamento de estados HTTP |
| `backend/services/__init__.py` | 1 | — |

### Frontend — 5642 linhas, das quais 4184 de JavaScript

| Ficheiro | Linhas | Papel |
| --- | --- | --- |
| `frontend/styles.css` | 1394 | Todo o aspecto visual; sem dependências externas |
| `frontend/js/ui.js` | 363 | Fabrico de DOM: `el`, `card`, `banner`, `toast`, `badge`, campos, `skeletonStack` |
| `frontend/js/generator.js` | 296 | Painel de progresso do fluxo de um clique: etapas, barra, *poll* |
| `frontend/js/views/create.js` | 297 | Vista «Criar» — onde se escreve o tema |
| `frontend/js/views/generate.js` | 278 | Vista «Gerar» — briefing e submeter o `POST /api/generate` |
| `frontend/js/views/shorts.js` | 276 | Vista «Formatos curtos» |
| `frontend/js/composition.js` | 263 | Formulário de composição, partilhado por «Criar» e «Estúdio» |
| `frontend/js/api.js` | 258 | A única costura de rede: `apiFetch`, `apiJson` e um *wrapper* por rota |
| `frontend/js/views/settings.js` | 241 | Vista «Definições» — chaves e sondas de diagnóstico |
| `frontend/js/views/studio.js` | 213 | Vista «Estúdio» — composição e render de um projecto |
| `frontend/js/views/projects.js` | 197 | Vista «Projetos» — inventário e histórico de jobs de um clique |
| `frontend/js/main.js` | 198 | Router por *hash*, ciclo de vida das vistas, erro global |
| `frontend/js/pickers/music.js` | 185 | Selector de ambiente e faixa |
| `frontend/js/pickers/voices.js` | 167 | Selector de voz, agrupado por locale |
| `frontend/js/views/dashboard.js` | 167 | Vista «Painel» — o que o servidor consegue fazer e o que já foi feito |
| `frontend/js/pickers/subtitle.js` | 155 | Editor de estilo de legenda: cor, tamanho, posição, modo |
| `frontend/js/state.js` | 120 | Store observável mínimo; `localStorage` para preferências |
| `frontend/js/catalog.js` | 102 | Cache preguiçosa dos catálogos partilhados entre vistas |
| `frontend/js/pickers/presets.js` | 56 | Selector de tema |
| `frontend/js/projects.js` | 53 | Agrupa ficheiros de `/api/projects` em linhas de projecto |
| `frontend/index.html` | 64 | Casca: barra lateral, *crumb*, contentor `#ds-view` |
| `frontend/auth-handler.js` | 299 | Contrato `AUTH_*` no navegador. **Não modificado**, carregado como script clássico |

Os 19 módulos de `frontend/js/` (excluindo `auth-handler.js`) somam 3885 linhas.

### Restante da árvore

| Ficheiro | Linhas |
| --- | --- |
| `tests/*.py` (17 ficheiros) | 8600 |
| `scripts/check_env.py` | 441 |
| `Dockerfile` | 353 |
| `start.bat` | 165 |
| `docker-compose.yml` | 133 |
| `docker/entrypoint.sh` | 129 |
| `docker-compose.gpu.yml` | 83 |
| `start.sh` | 96 |
| `requirements.txt` | 56 |
| `.env.example` | 50 |
| `pytest.ini` | 24 |
| `Abrir Dark Video Studio.bat` | 10 |

---

## 2. A costura

O desenho tem um só objectivo estrutural: **`backend.app` é a única costura que os testes tocam.**

Os serviços são importados como **nomes**, não como módulos (`backend/app.py:20`), e o comentário no topo do ficheiro explica porquê: assim `mock.patch.object(app_module, "generate_script")` é a única indirecção entre a API e a camada de serviços.

`generator` é a excepção deliberada: é importado como **módulo** (`backend/app.py:76`), porque o registo de jobs é estado ao nível do módulo e toda a leitura e escrita tem de passar pelo mesmo objecto.

`viral_pipeline` é a segunda excepção: importado **preguiçosamente**, dentro do corpo de `build_viral` (`backend/app.py:745`). Precisa de `librosa`, e um import no topo do módulo fazia o servidor inteiro não arrancar sem ele. Sem o `librosa`, a rota devolve `503` com uma mensagem que diz como o instalar (`backend/app.py:746`).

O frontend segue a mesma ideia. `frontend/js/api.js` é a única costura de rede, com `API_BASE` igual à string vazia (`frontend/js/api.js:13`), porque a página é servida na mesma origem que a API. Não há CORS, não há *preflight*, não há host fixo no código.

---

## 3. O pipeline, etapa a etapa

### 3.1 O caminho de um clique

`generator.run_generation` (`generator.py:1020`) conduz quatro etapas. O progresso é escrito por `_stage` (`generator.py:684`), que só mexe em `stage`, `progress` e `message` — **nunca** em `status`.

| # | Etapa | Progresso | Chamadas |
| --- | --- | --- | --- |
| 1 | `script` | `0.05` | `script_gen.generate_script` |
| 2 | `voice` | `0.20` | `tts.synthesize_speech_long`, `pipeline.build_srt_from_text` sobre `script.full_text`, escrita do SRT |
| 3 | `media` | `0.40` | `pipeline.build_storyboard_from_segments`, `pipeline.search_media_for_scenes`, `pipeline.search_media_for_keywords` |
| 4 | `render` | `0.60` | `style.resolve_preset_and_style`, `music.pick_track`, `render_engine.render_video_hyperframes` |
| 5 | `done` | `1.00` | resultado anexado ao job; `status` passa a `completed` |

Antes da etapa 1, `_project_slug` (`generator.py:133`) deriva o nome do projecto a partir de `project_prefix`, ou do tema. `slugify_topic` (`generator.py:224`) normaliza com NFKD, decompõe os acentos e reduz tudo o que não for alfanumérico a um hífen. O resultado nunca tem separadores de caminho nem `..`.

**A degradação nunca é uma excepção.** Qualquer falha de etapa passa por `_fail` (`generator.py:712`), que escreve `status: "failed"`, uma mensagem em português e `progress: 0.0`, e devolve `{}`. O `_run_job` tem uma segunda rede (`generator.py:612`) para o caso de a excepção escapar do `run_generation`.

Um `AuthError` é desembrulhado para `CODIGO: mensagem (HTTP n)` (`generator.py:641`), para que o código sobreviva à travessia do pipeline em vez de ficar perdido dentro de um prefixo genérico.

O `render_engine` não levanta quando o HyperFrames falha: devolve `status: "error"`. Por isso o pipeline trata esse caso como falha de etapa (`generator.py:1135`), para o job não acabar `completed` com um resultado partido.

**O que a tabela não diz.** Três decisões pesam mais do que a lista de chamadas:

- **As legendas vêm do guião, não do áudio.** O gerador já tem `script.full_text` — a string exacta que o TTS leu — e deriva o SRT dela com `pipeline.build_srt_from_text` (`pipeline.py:377`) dentro de `_build_captions` (`generator.py:857`). Reconhecer a fala de um áudio sintetizado a partir dessa mesma string custa um modelo carregado mais uma descodificação inteira para devolver uma cópia pior do texto que já está na mão.
- **Nada bloqueia o loop.** Toda a chamada bloqueante passa por `asyncio.to_thread`, e os serviços corrotina cujo corpo ainda bloqueia — TTS e render — por `_off_loop` (`generator.py:205`), que os conduz num loop privado dentro de um *thread*. É isto que tirou a paragem de `GET /api/jobs/{id}`: os 4,176 s de antes passaram a 25 ms de latência máxima de *poll* no mesmo fluxo.
- **`output_stem` é único por job** (`generator.py:175`): o *slug* do tema mais o id do job, por omissão, com `_dedupe_output_stem` (`generator.py:157`) a resolver o resto. Antes, dois jobs do mesmo tema escreviam o mesmo MP4 e partilhavam o mesmo directório de trabalho do HyperFrames. Um `project_prefix` explícito continua a mandar: quem dá o nome é que decide.

O resultado fecha com `degraded` a fundir o estado das legendas com a origem do guião, e com `duration_report` (`generator.py:967`), que compara a duração entregue com `duration_target` e diz em português porque é que falhou quando falha: `duration_target` dimensiona o *guião*, e a duração entregue segue a narração já sintetizada. Numa corrida real, um alvo de 20 s deu 23,87 s — e é isso que o campo diz, não um número redondo.

### 3.2 O caminho manual

O caminho manual é o mesmo, com o utilizador a controlar os passos e a WebUI a fazer o *round-trip* entre eles:

```
POST /api/script       ->  guião, guardado em storage/uploads/<nome>.script.json
POST /api/tts          ->  MP3 + transcrição + storage/uploads/<nome>.srt
POST /api/build-video  ->  pesquisa de media + composição + HyperFrames + MP4
```

O truque que liga os dois: quando `POST /api/script` recebe `project_name`, guarda o guião em disco (`backend/app.py:177`). Depois, `POST /api/tts` com `text` vazio lê-o de volta (`backend/app.py:188`) e narra sem o cliente ter de reenviar o texto todo. Se não houver `full_text`, reconstrói a partir do gancho mais as secções.

`POST /api/build-video` exige que o `.srt` exista (`backend/app.py:604`) — é o ficheiro que o `pipeline` consegue sempre interpretar, mesmo quando o que lá dentro é um aviso de transcrição em falta e não fala.

### 3.3 A transcrição não fabrica

Isto foi, durante muito tempo, o ponto mais fácil de não ver deste projecto, e o pior.

O que lá estava: um `except Exception: pass` à volta do `faster-whisper` e, logo a seguir, cinco frases fixas em português, cada uma com três segundos de tempos. Duas condições diferentes caíam no mesmo caminho — o pacote em falta e o modelo a devolver zero segmentos — e as duas produziam o mesmo SRT de 15 s, sem uma única palavra de som, com `200` e sem campo nenhum de aviso. Foi assim que um vídeo de 27 MB com a narração correcta acabou com legendas sobre deslocações e ioga, a reportar `degraded: false`.

O contrato actual (`pipeline.py:195`) tem três saídas, e nenhuma delas é conteúdo inventado:

- **segmentos reais** — nenhum segmento marcado, e `transcription_status(segments)["degraded"]` é `False`;
- **`faster-whisper` em falta** — um único segmento com `placeholder: true` (`pipeline.py:104`), `placeholder_reason`, uma mensagem em português que diz que aquilo não é a fala, e os tempos limitados à duração real do áudio (`_placeholder_transcript`, `pipeline.py:169`); com `allow_placeholder=False` levanta `TranscriptionDependencyMissing` em vez disso;
- **o modelo correu e falhou, ou não devolveu nada** — `TranscriptionFailed` (`pipeline.py:149`), com o erro verdadeiro. Nunca engolido, nunca trocado por enchimento.

`is_placeholder_transcript` (`pipeline.py:258`) e `transcription_status` (`pipeline.py:279`) existem para o chamador ter de *olhar* para o resultado em vez de o assumir; a segunda devolve o par `degraded` mais `fallback_error` que a WebUI já lê.

Onde isto ainda pesa: o caminho manual. `/api/tts` transcreve a narração que acabou de sintetizar (`backend/app.py:436`) — o texto pode ter vindo do cliente e não do guião guardado, por isso não há texto fiável para partir em legendas. Uma falha real do modelo é `422` (`backend/app.py:441`); sem o pacote, o SRT escrito é o aviso de uma linha, e diz isso na própria legenda. O fluxo de um clique nem chega aqui: deriva as legendas do guião, como em [3.1](#31-o-caminho-de-um-clique).

### 3.4 Pesquisa de media por cena

`search_media_for_scenes` (`pipeline.py:913`) prefere termos escritos pelo modelo (`pipeline.py:847`) e, sem modelo, extrai-os da legenda da própria cena (`pipeline.py:591`). É a diferença entre um vídeo em que cada imagem corresponde ao que está a ser dito e um vídeo em que todas as cenas reciclam a mesma lista global.

Em `POST /api/build-video` a escolha é explícita (`backend/app.py:657`): o pool por cena ganha, e o pool global só é usado quando não há nenhum.

`asset_quality.looks_padded` (`asset_quality.py:70`) rejeita a seguir as imagens com moldura branca. Mede a luminância média e o mínimo e máximo a duas profundidades de cada borda: uma faixa quase branca e uniforme numa imagem que não é é a assinatura de um *padding* de stock. O asset rejeitado passa a candidato seguinte, e a lista dos rejeitados entra em `render.rejected_assets`.

---

## 4. Jobs e fila

### 4.1 O registo

`generator` mantém o estado ao nível do módulo (`generator.py:417`):

```python
_jobs: Dict[str, Job] = {}
_lock = asyncio.Lock()
_slot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_running_ids: set = set()
_next_seq: int = 0
```

`MAX_CONCURRENT_JOBS = 2` (`generator.py:70`): cinco renders ao mesmo tempo esgotariam a memória de uma máquina normal. O semáforo é criado ao nível do módulo e reutilizado entre event loops, porque o `pytest-asyncio` cria um por teste.

O registo é limitado por dois lados, em `_prune_locked` (`generator.py:443`): jobs terminados com mais de uma hora são removidos, e há um tecto duro de `MAX_JOB_HISTORY = 200`. Jobs em fila ou a correr **nunca** são podados, ou o servidor perderia trabalho.

As leituras públicas devolvem cópias profundas. `get_job` (`generator.py:479`) e `list_jobs` (`generator.py:485`) usam `_copy_job` (`generator.py:495`), e `Job.to_dict` (`generator.py:394`) faz um round-trip por JSON dos campos mutáveis. Um cliente não consegue mexer no registo pelo que recebeu.

A ordenação usa `updated_at` **e** `_seq` (`generator.py:487`). Os timestamps ISO-8601 só têm granularidade de segundo, por isso dois jobs submetidos no mesmo segundo empatariam; o contador monotónico de inserção é o desempate estável.

### 4.2 Submeter

`submit_job` (`generator.py:540`) valida, regista um job `queued` e devolve **imediatamente**. A validação é `GenerationRequest.validate` (`generator.py:279`), que devolve uma cópia com as omissões preenchidas e todos os campos dentro dos limites, levantando `ValueError` com mensagem em português no primeiro problema.

Depois, se houver um event loop a correr, `loop.create_task` agenda o worker. Se não houver — um teste, um script síncrono — o job fica em fila e é o chamador que tem de conduzir `_run_job`. É por isso que `POST /api/generate` é `async def`: a rota tem de correr no loop, senão o `create_task` não tem onde aterrar e o job ficava para sempre em fila.

### 4.3 O corpo do pedido é filtrado

`backend/app.py:789` lê o conjunto de campos permitidos do próprio dataclass em tempo de importação:

```python
GENERATION_PARAM_FIELDS: frozenset[str] = frozenset(
    field.name for field in dataclass_fields(generator.GenerationRequest)
)
```

`_generation_params` (`backend/app.py:794`) deixa passar só essas chaves. O motivo está no docstring: `submit_job` faz `GenerationRequest(**params)`, portanto uma chave estranha de um cliente mais antigo ou escrito à mão voltaria como `TypeError`. Filtrar aqui é a diferença entre um `202` e um `400` para um cliente que não controlamos. Verificado: `{"topic": "...", "bogus_key": "x", "mood": "dark"}` devolve `202`.

### 4.4 O estado `running` é real

`Job.status` só toma os valores `queued`, `running`, `completed`, `failed` e `cancelled`, e a máquina de estados é monotónica: `queued -> running -> completed | failed`, mais a saída `queued -> cancelled`.

**`_stage` nunca mexe em `status`** (`generator.py:684`): só em `stage`, `progress` e `message`. Quem escreve o estado é o *worker*, nas duas linhas a seguir a obter uma das *slots* do semáforo — `_running_ids.add(job_id)` e `job.status = "running"` (`generator.py:626-627`) — com a primeira marca de etapa logo a seguir (`generator.py:628`). Um job à espera de *slot* fica `queued`, com `progress: 0.0` e a mensagem de fila; por isso quem lê o snapshot distingue «à espera» de «a trabalhar» pelo `status`, sem adivinhar pelo `stage`.

É isso que dá a `cancel_job` (`generator.py:515`) uma garantia real. Ele recusa tudo o que não esteja `queued` (`generator.py:527-528`), portanto um job `running` devolve `False` e o `DELETE` responde `{"cancelled": false}`. Verificado em execução: com três jobs submetidos, dois passam a `running` e o terceiro fica `queued`; cancelar o terceiro dá `cancelled`, cancelar um dos dois não muda nada, e os dois completam. Não é uma falha do servidor — não existe interruptor partilhado, e interromper o HyperFrames a meio deixaria um MP4 truncado em `storage/outputs/`.

O conjunto `_running_ids` (`generator.py:425`) serve ao *worker* para saber o que está dentro do semáforo; o `cancel_job` não o consulta. Decide pelo `status`, que é a mesma informação já publicada no snapshot.

O painel reproduz a mesma distinção: `frontend/js/generator.js:39` mapeia `queued` para «inactivo» antes de olhar para a etapa, enquanto `running` mapeia para a etapa real do pipeline (`frontend/js/generator.js:40-47`), com o rótulo de estado «A trabalhar».

A falta de interruptor partilhado está listada como limitação em [README.md](../README.md#limitações-conhecidas), não escondido aqui.

### 4.5 Poda e histórico

`GET /api/jobs` devolve **um array simples**, não um envelope. É a única rota do servidor com essa forma, e o próprio frontend tem de se guardingar disso (`frontend/js/api.js:183` documenta-o explicitamente, e a vista de projectos verifica `Array.isArray` antes de iterar). Um cliente que leia `resposta.jobs` obtém `undefined` em silêncio.

---

## 5. O caminho do render

`render_engine.render_video_hyperframes` (`render_engine.py:1236`) é o ponto de entrada. É `async` e devolve sempre um *dict*: `status: "rendered"` ou `status: "error"` com a mensagem. **Não levanta** quando o render falha — o que obriga o pipeline a verificar `status` explicitamente.

### 5.1 As sete etapas

1. **Resolver o storyboard.** Se não vier nenhum, `parse_srt_to_segments` e `build_storyboard_from_segments` tratam disso (`render_engine.py:1274`). Se mesmo assim ficar vazio, há uma cena de recurso, para que o HyperFrames tenha alguma coisa para renderizar.
2. **Ajustar à duração do áudio.** Com `audio_duration` conhecida, `pipeline.fit_storyboard_to_duration` (`pipeline.py:1083`) redistribui as cenas para o áudio, e não o contrário.
3. **Preparar o directório.** `create_project_dir` (`render_engine.py:665`) cria `storage/outputs/.hf_<nome>/`, **apaga-o primeiro** para que assets de uma execução anterior nunca vazeiem, e escreve `hyperframes.json` e `package.json` com o *script* de render já fixado à versão.
4. **Copiar os assets.** `stage_project_assets` (`render_engine.py:370`) copia media e áudio para `<project_dir>/assets/` e devolve referências relativas. É obrigatório: o HyperFrames recusa recursos locais fora do directório do projecto. Testa cada imagem com `looks_padded` (`render_engine.py:411`) e recolhe as rejeitadas.
5. **Gerar o HTML.** `generate_composition_html` (`render_engine.py:483`) emite a composição com `build_subtitle_css` (`render_engine.py:183`) a aplicar o `SubtitleStyle` resolvido por `style.resolve_preset_and_style` (`style.py:611`). As animações por cena estão em `_scene_media_animations` (`render_engine.py:281`).
6. **Renderizar.** `render_with_hyperframes` (`render_engine.py:892`) corre `npx --yes hyperframes@0.8.92 render` com uma lista de argumentos — nunca uma *shell string*. A espera por esse processo é limitada pelo watchdog de [5.4](#54-o-watchdog-do-render). Se o `npx` não existir, traduz para `RuntimeError("npx not found...")`.
7. **Misturar a música.** `_apply_background_music` (`render_engine.py:1166`) só corre **depois** de um render bem-sucedido, porque o HyperFrames já meteu a narração no MP4. `mix_audio_track` (`render_engine.py:1072`) volta a correr o `ffmpeg` sobre o ficheiro acabado e substitui-o atomicamente.

`cleanup_render_dirs` (`render_engine.py:702`) remove os `.hf_*` das execuções anteriores, mantendo o que acabou de ser usado — útil para inspeccionar a composição.

### 5.2 A mistura de áudio

O grafo de filtro é escolhido por três funções, todas em `render_engine.py`:

- `_ducked_mix_filter` (`render_engine.py:1025`) — voz e cama musical, com a música a ser comprimida por *sidechain* a partir da própria voz. A voz é partida com `asplit` porque é simultaneamente entrada da mistura e chave do *sidechain*.
- `_flat_mix_filter` (`render_engine.py:1046`) — as duas entradas somadas nos seus próprios níveis, sem *ducking*. Aqui a voz **não** é partida: uma segunda saída `asplit` por consumir faz o `ffmpeg` abortar.
- `_music_only_filter` (`render_engine.py:1066`) — só quando `_has_audio_stream` (`render_engine.py:977`) confirma que o vídeo não tem áudio.

`_has_audio_stream` distingue três casos, não dois: `True`, `False` e `None`. `None` significa que o `ffprobe` não conseguiu responder, e isso **não** pode ser confundido com «o vídeo não tem áudio» — o autor do código comenta-o explicitamente.

Os objectivos de nível são centralizados: voz a −6 dBFS, música a −18 dBFS (`music.py:59`), com a biblioteca a normalizar cada faixa para −3 dBFS de pico (`music.py:46`), o que faz um `music_volume` linear de 0.18 assentar perto do alvo.

### 5.3 O motor de render

`HYPERFRAMES_CLI` (`render_engine.py:53`) é `npx.cmd` no Windows e `npx` nos restantes, e `HYPERFRAMES_VERSION` (`render_engine.py:54`) está fixo em `0.8.92`. A versão aparece em três sítios — o comando, o `scripts.render` e o `scripts.check` do `package.json` — e é a mesma constante nos três.

As dimensões vêm de `get_dimensions` (`render_engine.py:233`): `vertical` 1080x1920, `square` 1080x1080, `landscape` 1920x1080, com `vertical` como recurso para qualquer valor desconhecido.

O `_grain_overlay` (`render_engine.py:252`) e a `_progress_bar` (`render_engine.py:271`) usam **passos absolutos em tempos absolutos**, não `infinite`. É uma decisão deliberada: um efeito `infinite` daria um resultado diferente em cada frame, e o render tem de ser reprodutível.

### 5.4 O watchdog do render

O `npx hyperframes` é o passo lento e o único sem limite natural, por isso a espera por ele é limitada. `render_timeout_seconds` (`render_engine.py:726`) lê `DARK_STUDIO_RENDER_TIMEOUT` do ambiente a cada chamada (`render_engine.py:914`), nunca à importação: assim um teste pode encolher o prazo e um operador pode aumentá-lo sem reiniciar o servidor. Um valor em falta, vazio, não numérico ou não positivo cai no `DEFAULT_RENDER_TIMEOUT = 900.0` (`render_engine.py:356`).

O default veio de uma medição, não de um palpite: ~316 s para um corte de 41 s a 1080x1920 (1237 frames a ~3,9 fps) numa máquina de 2 vCPU. 900 s são cerca de três vezes isso, pelo que o watchdog só apanha um Chrome ou um `ffmpeg` encravado — nunca um render válido mas lento.

Quando o prazo estourar, `_kill_process_tree` (`render_engine.py:874`) derruba a **árvore toda**, não o filho directo: `os.killpg` com SIGTERM e depois SIGKILL no POSIX (`render_engine.py:798`), `taskkill /T /F` no Windows (`render_engine.py:823`). O `npx` deixa o Chrome e o `ffmpeg` a escrever num ficheiro que já ninguém espera, por que razão o filho é lançado em sessão própria (`render_engine.py:926`) e ficar só por ele deixaria processos órfãos. O `CancelledError` — que deriva de `BaseException` e por isso nunca chega ao `except Exception` do chamador — mata a árvore e volta a levantar (`render_engine.py:953`).

Para quem chama, o efeito é o de uma falha normal. `render_video_hyperframes` apanha a excepção e devolve `status: "error"` com a mensagem portuguesa «tempo limite de render excedido (900s). O render foi encerrado; reduza a duracao do video ou aumente DARK_STUDIO_RENDER_TIMEOUT.» (`render_engine.py:948`), o ficheiro parcial é apagado (`render_engine.py:947`) — um MP4 truncado que parece entregável é pior do que nenhum ficheiro — e o job do orquestrador acaba `failed` pelo caminho normal de etapa. **Nunca** é devolvido como `status: "rendered"`.

---

## 6. O grafo de módulos do frontend

Módulos ES nativos. `index.html` carrega `auth-handler.js` como script clássico (`frontend/index.html:19`) e `js/main.js` como módulo (`frontend/index.html:20`). Não há *bundler*, não há *transpiler*, não há CDN: os caminhos são relativos, para a página ser servida de `/app/` sem configuração.

```
main.js ──┬── ui.js
          ├── api.js
          ├── state.js
          ├── catalog.js
          └── views/  (7 vistas, todas registadas em ROUTES)
                 ├── dashboard.js ── catalog.js, projects.js, api.js, ui.js
                 ├── generate.js  ── catalog.js, pickers/presets.js, generator.js
                 ├── create.js    ── catalog.js, pickers/voices.js, composition.js
                 ├── studio.js    ── catalog.js, composition.js
                 ├── projects.js  ── projects.js, api.js, state.js
                 ├── shorts.js    ── api.js, state.js
                 └── settings.js  ── catalog.js, api.js

composition.js ── pickers/presets.js, pickers/subtitle.js, pickers/music.js
pickers/voices.js ── catalog.js
pickers/music.js   ── catalog.js
generator.js       ── ui.js, api.js, state.js
catalog.js         ── api.js, state.js
```

Três regras que o grafo respeita sem excepções:

- **As vistas nunca falam entre si.** Recebem um `ctx` com `signal`, `navigate`, `params` e `route`.
- **Só `api.js` toca na rede.** `catalog.js` e as vistas consomem os *wrappers* de `api.js`.
- **Só `state.js` escreve no estado.** `patch` e `setIn` são as únicas portas de entrada.

### 6.1 O router

`main.js:21` tem uma tabela `ROUTES` com as sete vistas, cada uma com `id`, `label`, `icon` e `render`. O *hash* é a assinatura da vista, não apenas o identificador: navegar de `/studio` para `/studio?project=outro` tem de remontar, senão o novo projecto seria ignorado (`frontend/js/main.js:99`).

Ao trocar de vista, `render` (`frontend/js/main.js:96`) chama `api.abortAll()`, que aborta todos os pedidos em voo, e depois chama o `dispose` da vista anterior. É por isso que nenhum módulo de vista precisa de seardown de temporizadores por conta própria.

`boot` (`frontend/js/main.js:137`) aquece os catálogos de presets e idiomas em paralelo antes do primeiro *paint*, para que os selectores apareçam de imediato.

### 6.2 As preferências

`state.js:75` define o que é persistido: `project`, `voice`, `style`, `music` e `preset`, sob a chave `darkstudio.prefs.v1` (`state.js:9`). Tudo o resto — `script`, `segments`, `storyboard`, `render`, `jobs` — é ** deliberadamente** não persistido, com o comentário do ficheiro a dar a razão: dados do servidor envelhecem, e a interface nunca deve mostrar um número fabricado.

`persist` (`state.js:78`) está dentro de um `try`/`catch`: em modo privado ou com a cota cheia, as preferências simplesmente não sobrevivem. Um armazenamento corrompido também não impede a aplicação de arrancar (`state.js:69`).

### 6.3 Onde as vistas descobrem o que existe

Duas sondas `HEAD` replacing uma chamada completa:

- `hasRenderedVideo` (`frontend/js/api.js:236`) — um `HEAD` contra a mesma rota que o elemento `<video>` usa. `200` significa renderizado, `404` significa nunca gerado. É silenciosa de propósito: um `404` aqui é a resposta, não uma falha a reportar.
- `probeGenerateRoute` (`frontend/js/api.js:251`) — pergunta se `POST /api/generate` está montado, **antes** de o utilizador carregar no botão. Uma rota que existe mas só aceita `POST` responde `405`; uma rota que não existe responde `404`.

---

## 7. Layout de dados

Tudo o que o utilizador não pode perder vive sob `storage/`, na raiz do projecto (`pipeline.py:35`). As cinco subpastas são as mesmas que o verificador cria e que o entrypoint do contentor garante (`scripts/check_env.py:80`, `docker/entrypoint.sh:55`).

```
storage/
├── uploads/        o que o utilizador traz e o que o pipeline intermédio produz
│   ├── <projeto>.mp3              narração sintetizada, ou áudio carregado
│   ├── <projeto>.srt              legendas com tempos
│   ├── <projeto>.script.json      guião guardado por POST /api/script
│   └── music_upload_<uuid>.<ext>  área de passagem de um upload, já apagada
├── outputs/        os renders
│   ├── <projeto>.mp4              o vídeo principal
│   ├── <projeto>_shorts.mp4       o cut de formato curto
│   ├── <projeto>.json             o resumo do render, tal como a API o devolveu
│   └── .hf_<projeto>/             directório de trabalho do HyperFrames
│       ├── index.html             a composição gerada
│       ├── hyperframes.json
│       ├── package.json
│       └── assets/                media e áudio copiados
├── media/          media de stock descarregada, por projecto
├── thumbnails/     thumbnails do pipeline viral
└── music/          a biblioteca musical
    ├── uploads.json               manifesto dos uploads do utilizador
    └── <faixa>.mp3|.wav
```

Três decisões merecem explicação:

**O directório do HyperFrames vive em `outputs/`, não num temporário.** O comentário em `create_project_dir` (`render_engine.py:668`) diz porquê: um temporário convida o Chrome/Puppeteer a competir com a limpeza.

**Um SRT é o contrato entre etapas.** `POST /api/build-video` recusa sem ele (`backend/app.py:604`), e `/api/tts` escreve-o logo a seguir à narração. O que mudou é o conteúdo: nunca é inventado em silêncio — ou é a transcrição, ou é um aviso de uma linha marcado como tal ([3.3](#33-a-transcrição-não-fabrica)), ou o pedido falha com `422`.

**A lista de projectos é derivada, não armazenada.** Não existe base de dados de projectos. `GET /api/projects` (`backend/app.py:1013`) lista os ficheiros de `storage/uploads/`, e é o frontend que os agrupa por nome antes de os mostrar (`frontend/js/projects.js:13`). Um projecto é, operacionalmente, «os ficheiros que partilham um mesmo radical».

### 7.1 O `.env`

O `.env` vive na raiz e é escrito por `POST /api/settings` (`backend/app.py:952`), que o reescreve por inteiro a partir do modelo `ApiSettings` e actualiza também `os.environ` para o processo corrente. As chaves nunca são devolvidas pela API — o `check_env.py` também nunca as imprime, só diz se estão presentes.

Um efeito lateral que vale a pena saber: `POST /api/settings` reescreve o ficheiro com o conjunto fixo de nove chaves. Uma linha que não seja uma dessas, no `.env`, desaparece quando a vista de definições grava.
