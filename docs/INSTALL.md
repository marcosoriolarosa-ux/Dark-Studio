# Instalação

Instalação nativa no Windows (plataforma de referência), Linux/macOS e Docker. Para a superfície HTTP ver [API.md](API.md). Para o desenho ver [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 0. Antes de começar

Três factos que determinam o resto do guia:

- **A raiz não serve a interface.** O backend monta a pasta `frontend/` em `/app` (`backend/app.py:114-116`) e **não existe rota em `/`**. `http://127.0.0.1:8013/` devolve `404`, verificado em execução. A URL da aplicação é `http://127.0.0.1:<porta>/app/`.
- **A porta nunca é fixa.** O valor por omissão é 8013, mas os dois atalhos procuram a primeira porta livre acima dele. A porta que vai ser usada é impressa no passo `[3/4]`.
- **Não há autenticação.** O servidor escuta em `127.0.0.1` e qualquer processo local o pode chamar. É uma aplicação de utilizador único; não exponha a porta a uma rede.

---

## 1. Windows

### 1.1 Python 3.10 ou superior

Download em <https://www.python.org/downloads/>. No instalador, **marque "Add python.exe to PATH"**. Verifique:

```bat
python --version
```

O `start.bat` aceita `python`, `py -3` ou o `.venv` do projecto, por esta ordem de preferência (`start.bat:66-75`). Se nenhum responder, o atalho pára com uma mensagem a pedir o Python 3.10+ (`start.bat:82`).

### 1.2 ffmpeg no PATH

`ffmpeg` **e** `ffprobe` são dois executáveis que vêm no mesmo pacote. Não basta ter um deles.

```bat
winget install --id Gyan.FFmpeg -e
```

Alternativas: `choco install ffmpeg`, ou descompactar o ZIP de <https://www.gyan.dev/ffmpeg/builds/> para uma pasta sem acentos e acrescentar essa pasta ao `PATH`.

Verifique **ambos**:

```bat
ffmpeg -version
ffprobe -version
```

Sem eles o servidor arranca e a geração de vídeo falha a meio: `asset_quality.py` e `pipeline.py` correm-nos em `subprocess` para medir duração e qualidade, e `render_engine.py` precisa do `ffmpeg` para misturar a música.

### 1.3 Node.js LTS

O render não é Python: o motor chama `npx --yes hyperframes@0.8.92 render` (`render_engine.py:927-931`), que por sua vez lança Chrome headless. Sem `node`/`npx` não há vídeo.

Instale o **LTS** em <https://nodejs.org/en/download>. O Debian 12 traz Node 18, que já está em fim de vida; a imagem Docker usa explicitamente o 22.14.0 (`Dockerfile:23`).

```bat
node --version
npx --version
```

### 1.4 Dependências Python

```bat
cd C:\Dark-Studio
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

O `.venv` **tem de ser dentro da pasta do projecto**. É o primeiro sítio onde o `start.bat` procura o interpretador, e `check_env.py` só avisa, em vez de falhar, quando o Python é global.

O que o `pip` instala, e porque:

| Grupo | Pacotes | Observação |
| --- | --- | --- |
| Web | `fastapi`, `uvicorn[standard]`, `python-multipart`, `pydantic`, `httpx`, `python-dotenv` | Versões fixadas para instalações reprodutíveis |
| Áudio/vídeo | `numpy`, `librosa` | Pesados. O primeiro `import librosa` demora vários segundos (numba) |
| Transcrição | `faster-whisper` | **Opcional**: `check_env.py:50` marca-o `AVISO`, não `FALTA`, e o servidor arranca sem ele. Só é preciso para transcrever áudio que o servidor não escreveu — `POST /api/transcribe` e a SRT do `POST /api/tts`. Arrasta `ctranslate2` e `onnxruntime`: download grande. O caminho de um clique não o usa: deriva as legendas do texto do guião |
| Voz | `edge-tts` | A voz por omissão, sem chave. É o pacote mais importante depois do FastAPI |
| Testes | `pytest`, `pytest-asyncio`, `requests` | `pytest.ini` usa `asyncio_mode = auto`, que o `pytest-asyncio` fornece |

> **Nota sobre o `librosa`.** As linhas 18-22 do `requirements.txt` dizem que `backend/app.py` importa `viral_pipeline` no topo e que sem estes pacotes «o servidor NAO arranca de todo». **Isso já não é verdade**: o import é preguiçoso, dentro de `build_viral` (`backend/app.py:745`), precisamente para o servidor arrancar sem `librosa`. O que continua verdade: sem `librosa`, `POST /api/build-viral` devolve `503`. Por isso o `scripts/check_env.py:48` continua a marcá-lo como obrigatório.

### 1.5 Ficheiro `.env`

```bat
copy .env.example .env
```

Abra `.env` e preencha `OPENROUTER_API_KEY` (<https://openrouter.ai/keys>). Sem esta chave o servidor arranca, mas **toda** a geração de texto cai no guião local de recurso. As chaves de stock media (`PEXELS_API_KEY`, `PIXABAY_API_KEY`) são opcionais: sem elas só se usa material local.

Todas as chaves disponíveis, com o que cada uma liga:

| Variável | Serve | Sem ela |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | guião, estratégia, destaques de shorts | recurso local em tudo o que é texto |
| `OPENROUTER_MODEL` | modelo `:free` a usar | valor por omissão do `pipeline.DEFAULT_FREE_MODEL` |
| `PEXELS_API_KEY` | imagens de stock | só material local |
| `PIXABAY_API_KEY` | imagens e vídeo de stock | só material local |
| `GEMINI_API_KEY`, `OPENAI_API_KEY`, `YOUTUBE_API_KEY` | fornecedores alternativos de IA | ignorados |
| `AZURE_SPEECH_KEY` + `AZURE_SPEECH_REGION` | TTS da Azure | o `edge-tts` continua a ser o predefinido |
| `DARK_STUDIO_PORT` | porta base | 8013 |
| `DARK_STUDIO_PORT_MAX` | limite superior da busca de porta | porta base + 20 |
| `DARK_STUDIO_ALLOWED_ORIGINS` | origens CORS | `http://127.0.0.1:8013` e `http://localhost:8013` |
| `DARK_STUDIO_NO_BROWSER` | `1` impede a abertura do navegador | abre |
| `DARK_STUDIO_RENDER_TIMEOUT` | segundos que um render pode correr antes de ser morto | 900 |

O `DARK_STUDIO_RENDER_TIMEOUT` é lido do ambiente a cada render (`render_engine.py:726`), não uma vez no arranque: um valor em falta, vazio, não numérico ou não positivo vale 900. Passado o prazo, o render é morto como uma árvore de processos — `os.killpg` no POSIX, `taskkill /T /F` no Windows (`render_engine.py:823`) — e o resultado sai como uma falha comum, com mensagem em português, nunca como um MP4 truncado. Os 900 s por omissão são generosos de propósito: um render real de 41 s a 1080x1920 mediu 316 s numa máquina de 2 vCPUs (1237 frames a ~3,9 fps), e hardware mais lento escala a partir daí, por isso o watchdog só apanha um Chrome ou `ffmpeg` encravado — nunca um render lento mas válido (`render_engine.py:734-738`). Ver [O watchdog do render](ARCHITECTURE.md#54-o-watchdog-do-render).

O ficheiro é lido como UTF-8 e as chaves não devem ter acentos. **Nunca** coloque uma chave real no `.env.example`. O `.env` está no `.gitignore`, e a aplicação também o escreve sozinha quando usa `POST /api/settings`.

`OPENROUTER_MODEL` tem de terminar em `:free` — `POST /api/settings` recusa com `400` caso contrário, e o próprio cliente OpenRouter responde `404 "No endpoints found"` para modelos `:free` sem endpoint para a sua chave. Nesse caso, troque de modelo.

### 1.6 Correr o verificador

```bat
.venv\Scripts\python scripts\check_env.py --fix
```

O `--fix` cria as pastas de `storage/` em falta. Saída 0 se tudo o obrigatório passa, 1 caso contrário. As chaves de API nunca são impressas — o verificador só diz se estão presentes.

### 1.7 Arrancar

**Duplo clique em `Abrir Dark Video Studio.bat`**, ou a partir de uma consola, para ver os logs:

```bat
set DARK_STUDIO_PORT=9000
start.bat
```

Depois de alguns segundos o navegador abre em `http://127.0.0.1:<porta>/app/`. Se preferir não abrir:

```bat
set DARK_STUDIO_NO_BROWSER=1
start.bat
```

Para encerrar: `Ctrl+C` na janela.

---

## 2. Linux e macOS

```sh
sudo apt install ffmpeg          # Debian/Ubuntu
sudo apt install python3-venv python3-pip
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

cd ~/Dark-Studio
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env              # depois editar

.venv/bin/python scripts/check_env.py --fix
DARK_STUDIO_PORT=9000 sh start.sh
```

Em macOS, `brew install ffmpeg node`.

O `start.sh` é `sh`-compatível (sem `[[ ]]`, sem arrays), por isso funciona com o `sh` do sistema. Faz os mesmos quatro passos que o `start.bat`.

---

## 3. As chaves de variáveis que o arranque lê

| Variável | Efeito | Quem lê |
| --- | --- | --- |
| `DARK_STUDIO_PORT` | porta base; se estiver ocupada, procura a seguinte | `check_env.py:303` (quando não lhe é passada no comando), `start.bat:111` |
| `DARK_STUDIO_PORT_MAX` | limite superior da busca | `check_env.py:307` |
| `DARK_STUDIO_NO_BROWSER` | `1` não abre o navegador | `start.bat:150`, `start.sh:90` |
| `DARK_STUDIO_ALLOWED_ORIGINS` | lista CORS separada por vírgulas | `backend/app.py:92`, `backend/app.py:97` |

Nota sobre a sonda de portas: `start.bat:114` passa a porta base ao verificador como argumento; o `start.sh:65` **não** passa nada e deixa que o `check_env.py` leia a variável de ambiente. O resultado é o mesmo.

A sonda é um `bind()` em Python **sem** `SO_REUSEADDR` (`check_env.py:256`), não texto de `netstat`, precisamente para não confundir uma porta em `TIME_WAIT` com uma porta livre.

O CORS aceita, sem configuração, qualquer `localhost`, `127.0.0.1` ou `[::1]` em qualquer porta, por causa da expressão regular em `backend/app.py:91`. A lista explícita só é necessária em acesso remoto.

---

## 4. Ver o que está a acontecer

O preflight é a primeira coisa que corre e diz exactamente o que falta. As opções:

```sh
python scripts/check_env.py              # relatório
python scripts/check_env.py --fix        # cria as pastas de storage/ em falta
python scripts/check_env.py --strict     # avisos também falham (saída 1)
python scripts/check_env.py --import-check   # importa os pacotes em vez de os procurar (lento)
python scripts/check_env.py --pick-port  # imprime a primeira porta livre a partir de 8013
python scripts/check_env.py --check-port 8013   # imprime free ou busy
```

`--machine` muda a saída do modo de portas para pares `CHAVE=valor`, que é o que os dois atalhos consomem. As 29 verificações cobrem: versão do Python, se está num virtualenv, os dez pacotes Python (nove obrigatórios e o `faster-whisper` opcional, `check_env.py:40-51`), os três de teste, os quatro binários, a escrita em `storage/`, a presença do `.env` e as nove chaves.

Depois, para o servidor:

```bash
curl -s http://127.0.0.1:8013/health
curl -s http://127.0.0.1:8013/api/tts/status   # o que é que posso usar agora
curl -s http://127.0.0.1:8013/api/providers    # que fornecedores estão ligados
```

`GET /api/tts/status` e `GET /api/providers` devolvem sempre `200`. Um pacote opcional em falta é uma **funcionalidade degradada**, não um pedido falhado — é isso que o painel de definições mostra.

---

## 5. Docker

### 5.1 O estado real do caminho Docker

Verifiquei isto a sério. Resultado:

- **A imagem constrói.** `docker build .` completa os três estágios sem erro. Os dois `docker compose config` (base e com o override de GPU) validam sem queixas.
- **O contentor não arranca, por causa de uma única linha em falta.** `docker run` sai com código **126** e o log mostra:
  ```
  [dark-studio] preflight: a verificar o ambiente...
  /app/docker/entrypoint.sh: 72: /app/scripts/check_env.py: Permission denied
  ```
- **A causa é o bit de execução.** O `Dockerfile:283` faz `COPY scripts/ /app/scripts/` sem modo, e o ficheiro chega à imagem como `-rw-rw-rw-`. Mas `docker/entrypoint.sh:72` invoca-o **directamente** (`"$APP_DIR/scripts/check_env.py" --fix`), o que exige `+x`. Repare no contraste: o `Dockerfile:288` faz `chmod 0755` no `entrypoint.sh`, e o bit ficou lá — a mesma esqueceu-se do `check_env.py`.
- **Corri-o com `chmod` e o resto está inteiro.** Com o bit posto, o preflight passa (29 verificações, 0 falhas, 11 avisos — todos de chaves em falta), o uvicorn arranca em `0.0.0.0:8013`, e `/health` devolve `200`, `/app/` devolve `200`, `/docs` devolve `200`, `/` devolve `404`, `/api/tts/status` devolve as 53 vozes.

Como não posso tocar em ficheiros de código, a correcção fica aqui para quem a quiser aplicar. Uma linha no `Dockerfile`, depois do `COPY`:

```dockerfile
RUN chmod 0755 /app/scripts/check_env.py
```

ou, no mesmo `COPY`:

```dockerfile
COPY --chmod=0755 scripts/ /app/scripts/
```

Alternativa sem tocar no `Dockerfile`: montar o projecto por *bind mount* a partir de uma cópia onde o ficheiro já tem o bit, e arrancar com um `ENTRYPOINT` que use o `PATH` do virtualenv em vez da execução directa.

### 5.2 Correr depois da correcção

```bash
cp .env.example .env       # e preencher pelo menos OPENROUTER_API_KEY
docker compose up -d --build
```

A interface fica em <http://localhost:8013/app/>. Para ver os logs:

```bash
docker compose logs -f dark-studio
```

### 5.3 O que o compose faz, e porquê

`docker-compose.yml` (133 linhas) escolhe deliberadamente:

- **Porta `8013:8013`** (`docker-compose.yml:30`). A porta do contentor tem de bater certo com `DARK_STUDIO_PORT`, porque o entrypoint usa essa variável para ligar o uvicorn (`docker/entrypoint.sh:40`). As duas coisas mudam em conjunto.
- **`DARK_STUDIO_ALLOWED_ORIGINS` explícita** (`docker-compose.yml:48`). `http://127.0.0.1:8013` e `http://localhost:8013` são origens **diferentes** para o navegador, mesmo que apontem para a mesma máquina. Em acesso remoto, ponha a origem do cliente, e assegure-se de que o servidor está acessível nesse endereço.
- **Todas as chaves com `${VAR:-}`** (`docker-compose.yml:56`). O compose não falha quando uma chave não existe: passa a string vazia e é a aplicação que degrada. Um `environment: [PEXELS_API_KEY]` sem omissão faria o `docker compose up` abortar por causa de uma chave que a funcionalidade opcional nem usa.
- **Volume nomeado em `storage/`** (`docker-compose.yml:84`). É o único estado que não pode perder. Sem ele, `docker compose down` apaga tudo o que foi gerado. A pasta é criada dentro da imagem com o owner de `darkstudio` (uid 10001), e um volume vazio montado por cima herda esse owner — por isso o contentor escreve sem root. **Se um volume antigo tiver outro owner, o contentor aborta no arranque**; `docker compose down -v` resolve.
- **`init: true`** (`docker-compose.yml:99`). PID 1 é o `tini`. O render lança Chrome, que lança subprocess; sem recolher, o contentor acumula processos zombie.
- **`shm_size: "2gb"`** (`docker-compose.yml:121`). O `/dev/shm` do Docker tem 64 MB por omissão e o Chrome rebenta a escrever um frame de 1080x1920.
- **`mem_limit` desligado**, com a recomendação comentada (`docker-compose.yml:114`). Um render consome 2-3 GB; com dois, 5-6 GB não é exagerado. Fixar um número sem conhecer o host é a forma rápida de um OOMKill a meio.

### 5.4 GPU

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

**O que a GPU compra: a transcrição — mas, com o código como está, nem isso.** O `ctranslate2` sabe usar GPU, e o override define `WHISPER_DEVICE=cuda` (`docker-compose.gpu.yml:76`) e `WHISPER_COMPUTE_TYPE=float16` (`docker-compose.gpu.yml:78`). Só que `pipeline.transcribe_audio_file` instancia o modelo com `device="cpu"` e `compute_type="int8"` fixos no código (`pipeline.py:225`) e nunca lê essas duas variáveis: em todo o projecto, `WHISPER_DEVICE` e `WHISPER_COMPUTE_TYPE` só aparecem no próprio override. Um contentor com a VRAM reservada transcreve, portanto, em CPU — exactamente o modo de «instalei GPU e não ficou mais rápido» que o comentário do ficheiro descreve (`docker-compose.gpu.yml:71-75`). Some-se que `faster-whisper` é opcional e que o caminho de um clique não transcrece nada: o único trabalho que sobra para a GPU é transcrever áudio que o utilizador carregou.

**O que a GPU não compra: o render, que é a parte cara.** O HyperFrames compõe frames com Chrome headless, e o Chrome neste contentor corre com `--disable-gpu` no wrapper que o `Dockerfile` instala (`Dockerfile:264`). Não há aceleração de vídeo no caminho e, mesmo que houvesse, o encoder de saída é o `ffmpeg`, não o Chrome. Isto está escrito no próprio ficheiro (`docker-compose.gpu.yml:14-20`). Para renders mais rápidos, a resposta é mais CPU e mais RAM: um render medido de 41 s a 1080x1920 levou 316 s numa máquina de 2 vCPUs.

O custo é real: a base CUDA (`nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04`) ocupa 6-8 GB de imagem em vez dos ~2 GB da base Python. E o benefício é mais estreito do que parece: com o modelo fixado em CPU e o caminho de um clique a não transcrever, só compensa se a transcrição com whisper de áudios carregados for mesmo uso diário.

Pré-requisitos, a verificar na sua máquina e não assumidos pelo ficheiro: Docker 19.03+ com o runtime NVIDIA configurado, drivers 525+ e um contentor CUDA que arranque. Teste com:

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi
```

Se este comando falhar, o problema é o runtime ou os drivers, não este projecto.

### 5.5 Uma fonte de surpresas: as fontes

Os quatro pacotes de tipografia do `Dockerfile` — `fontconfig`, `fonts-liberation`, `fonts-dejavu-core`, `fonts-noto-color-emoji` (`Dockerfile:211-214`) — **não são opcionais**. A composição queima texto estilizado nos frames. Sem fontes, o Chrome renderiza com o que o `fontconfig` conseguir resolver, as legendas **desaparecem em silêncio**, e o vídeo sai com o áudio e a imagem certos e zero texto, sem qualquer erro em nenhum log. É o único item da imagem cuja ausência produz um resultado errado em vez de uma falha visível — e por isso está descrito como tal no próprio `Dockerfile`.

Numa instalação nativa, o mesmo risco existe em Linux: um sistema sem `fonts-liberation` não resolve «Arial» nem «Segoe UI».

---

## 6. Testes

```bash
python -m pip install -r requirements.txt     # inclui pytest, pytest-asyncio e requests
pytest -m "not live"
pytest -m "not live and not slow"              # salta os renders completos
```

> **Os testes `live` estão desligados por omissão.** O `pytest.ini` traz `addopts = -m "not live"` (`pytest.ini:21`) e declara o marcador `live` logo a seguir (`pytest.ini:24`), por isso um `pytest` a seco não chama a OpenRouter, a Pexels nem a Pixabay. Para os correr, peça-os à mão com `pytest -m live` — e saiba que esse caminho bate mesmo na rede e gasta quota real. As formas explícitas da tabela acima continuam válidas: o `-m` da linha de comandos vem depois do `addopts`, e é o último a valer.

Os marcadores disponíveis são `slow` e `live`. Os 16 ficheiros de `tests/` são executados com `asyncio_mode = auto`, sem configuração adicional.

Dois testes de ponta a ponta usam `BUILD_VIDEO_TIMEOUT = 900` (`tests/test_e2e.py:25`) num `POST /api/build-video` completo (`tests/test_e2e.py:213`, `tests/test_e2e.py:221`). Numa máquina mais lenta, um render não cabe em 900 s e o teste falha por tempo, não por defeito.

---

## 7. Resolução de problemas

### O navegador abre e mostra «Not Found»

Não está a usar `/app/`. A URL é `http://127.0.0.1:<porta>/app/`. A raiz responde `404` por desenho.

### «Nenhuma porta livre a partir de 8013»

O `netstat -ano -p tcp` impresso pelo `start.bat:120` mostra quem ocupa a porta. Feche o processo, ou mude a base:

```bat
set DARK_STUDIO_PORT=9000
start.bat
```

A busca vai de `DARK_STUDIO_PORT` até `DARK_STUDIO_PORT_MAX`, que por omissão é a base + 20.

### `o .venv existe mas nao executa`

A criação do virtualenv foi interrompida. Apague e refaça:

```bat
rmdir /s /q .venv
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

O `start.bat:66-69` testa o `.venv` antes de o usar, precisamente para não transformar isto num traceback mais adiante.

### `npx not found. Please install Node.js and npm.`

Falta o Node. Volte ao passo 1.3.

### «tempo limite de render excedido»

O render passou do prazo e foi morto. Num corte longo, ou numa máquina lenta, isso acontece: baixe o `duration_target` do pedido, reduza o `section_count`, ou aumente `DARK_STUDIO_RENDER_TIMEOUT` no `.env` (900 s por omissão). O ficheiro parcial é apagado de propósito — um MP4 truncado que pareça concluído é pior do que nenhum. Se o erro persistir com um vídeo curto, o Chrome ou o `ffmpeg` estão encravados; reinicie o servidor. E atenção: a vista «Definições» reescreve o `.env` com o seu conjunto fixo de nove chaves, por isso um valor posto à mão desapareceu assim que alguém gravou lá.

### A legenda não aparece no vídeo

Por ordem de probabilidade:

1. **Faltam fontes** (ver [5.5](#55-uma-fonte-de-surpresas-as-fontes)). Sintoma: imagem certa, zero texto, nenhum erro.
2. `include_captions` foi enviado como `false`, ou `subtitle_style` tem `"mode": "hidden"`.
3. O `.srt` está vazio ou tem uma única cena.

### As legendas não batem com a narração

Primeiro, o que é que está a correr. O caminho de um clique (`POST /api/generate`) **não usa `faster-whisper`**: já tem em mãos o texto exacto que o TTS leu (`script.full_text`) e deriva as legendas desse texto com `pipeline.build_srt_from_text` (`pipeline.py:377`), em vez de transcrever um áudio cujo texto conhece (`generator.py:857-870`). Num vídeo gerado por esse caminho as legendas erram porque a narração não corresponde ao guião guardado, ou porque os tempos são estimados — leia em `GET /api/jobs/{job_id}` os campos `caption_builder`, `caption_duration_source` e `caption_reasons`: dizem se as legendas saíram do `pipeline.build_srt_from_text` ou do corte local, e se a duração veio do `ffprobe` ou de uma estimativa a partir do número de palavras (`generator.py:897-909`, `generator.py:1174-1176`).

`faster-whisper` só entra em `POST /api/transcribe` (áudio carregado pelo utilizador) e na metade da SRT do `POST /api/tts`. Aí `transcribe_audio_file` já não engole nada, e tem três saídas distinguíveis (`pipeline.py:195-255`):

- **Transcrição real** — nenhum segmento leva marcador, `is_placeholder_transcript()` dá `False` (`pipeline.py:258`) e `transcription_status()` devolve `degraded: false` (`pipeline.py:279`).
- **`faster-whisper` em falta** — sai **um único** segmento com `placeholder: true`, `placeholder_reason: "faster-whisper-not-installed"` e uma `placeholder_message` em português a dizer que aquele texto não é a fala do áudio (`pipeline.py:104-133`, `pipeline.py:169-192`). A legenda é literalmente `[SEM TRANSCRIÇÃO: faster-whisper não instalado - pip install faster-whisper]`, por isso o aviso aparece no SRT e queimado no próprio vídeo. A marca de tempo é a duração real do áudio medida com o `ffprobe`, e só cai para 5 s se o `ffprobe` falhar (`pipeline.py:153-166`, `pipeline.py:138`). Com `allow_placeholder=False` o mesmo caso levanta `TranscriptionDependencyMissing` (`pipeline.py:221`).
- **`faster-whisper` instalado mas a transcrição falha** — levanta `TranscriptionFailed` com o erro real, nunca engolido nem trocado por texto inventado (`pipeline.py:149`, `pipeline.py:243-253`). `POST /api/transcribe` não apanha essa excepção, por isso responde `500`; `POST /api/tts` apanha-a e responde `422` com a mensagem do erro (`backend/app.py:444-446`).

Para confirmar se o pacote está cá, corra `python scripts/check_env.py`: a linha `pacote faster-whisper` aparece como `AVISO` quando falta. **`GET /api/tts/status` não diz nada sobre isto** — só reporta os fornecedores de TTS (`backend/app.py:367-390`).

### O guião sai genérico

Falta `OPENROUTER_API_KEY`, ou a quota diária dos modelos `:free` acabou. `GET /api/providers` diz qual dos dois é. O `POST /api/script` marca `degraded` verdadeiro e enche `fallback_error`, mas **devolve `200`** — se a sua aplicação não ler esse campo, a degradação é silenciosa.

### «Estado desconhecido» no painel de geração

O painel perdeu o contacto com o servidor a meio de um *poll*. As causas habituais são o servidor ter sido fechado, ou o terminal com o `uvicorn` ter morrido. O trabalho pode continuar no servidor: veja `GET /api/jobs` e o histórico na vista «Projetos». Se preferir acompanhar por script, use `GET /api/jobs/{job_id}` directamente.

### Erro de CORS

Só em acesso remoto. A origem tem de estar em `DARK_STUDIO_ALLOWED_ORIGINS`, com esquema e porta, exactamente igual à que o navegador mostra na barra de endereço. `localhost:8013` sem esquema não funciona.

### O contentor não arranca (Docker)

É quase certo o caso do ponto [5.1](#51-o-estado-real-do-caminho-docker): `/app/scripts/check_env.py: Permission denied`, saída 126. A correção é um `chmod`.

### O contentor aborta a escrever em `storage/`

O volume nomeado foi criado por outra versão da imagem, com outro owner. Recrie-o:

```bash
docker compose down -v
docker compose up -d
```

### `docker compose` falha com «service dark-studio has neither an image nor a build context»

Está a usar o override de GPU sozinho. Ele **não** é um compose completo:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

### A primeira chamada a `edge-tts` falha

O `edge-tts` usa um endpoint gratuito do Microsoft Edge. Se a máquina estiver atrás de uma rede que o bloqueie, a falha é `AUTH_REQUEST_FAILED` com HTTP 502. A alternativa é configurar `AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION`.
