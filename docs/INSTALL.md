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

O render não é Python: o motor chama `npx --yes hyperframes@0.8.92 render` (`render_engine.py:696`), que por sua vez lança Chrome headless. Sem `node`/`npx` não há vídeo.

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
| Transcrição | `faster-whisper` | Arrasta `ctranslate2` e `onnxruntime`: download grande. O servidor arranca sem ele |
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

`--machine` muda a saída do modo de portas para pares `CHAVE=valor`, que é o que os dois atalhos consomem. As 29 verificações cobrem: versão do Python, se está num virtualenv, os dez pacotes obrigatórios, os três de teste, os quatro binários, a escrita em `storage/`, a presença do `.env` e as nove chaves.

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

**O que a GPU compra: só a transcrição.** O `faster-whisper` pode correr em CUDA, porque o `ctranslate2` sabe usar GPU. O override define `WHISPER_DEVICE=cuda` e `WHISPER_COMPUTE_TYPE=float16` (`docker-compose.gpu.yml:76`).

**O que a GPU não compra: o render.** O HyperFrames compõe frames com Chrome headless, e o Chrome neste contentor corre com `--disable-gpu`. Não há aceleração de vídeo no caminho e, mesmo que houvesse, o encoder de saída é o `ffmpeg`, não o Chrome. Isto está escrito no próprio ficheiro (`docker-compose.gpu.yml:14`). Para renders mais rápidos, a resposta é mais CPU e mais RAM.

O custo é real: a base CUDA (`nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04`) ocupa 6-8 GB de imagem em vez dos ~2 GB da base Python. Só compensa se a transcrição com whisper for uso diário.

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

> **`-m "not live"` não é opcional.** O `pytest.ini` declara o marcador `live` e diz como o desmarcar (`pytest.ini:7`), mas **não tem `addopts`**. Um `pytest` sem `-m` chama a OpenRouter e a Pexels a sério e gasta quota real. Isto vale a pena porque o cabeçalho de `tests/test_live_providers.py` afirma que os testes estão «Deselected by default» — não estão. Corrigir isto é uma linha: `addopts = -m "not live"` no `pytest.ini`.

Os marcadores disponíveis são `slow` e `live`. Os 16 ficheiros de `tests/` são executados com `asyncio_mode = auto`, sem configuração adicional.

Dois testes de ponta a ponta usam `timeout=120` num `POST /api/build-video` completo (`tests/test_e2e.py:209`, `tests/test_e2e.py:217`). Numa máquina mais lenta, um render não cabe em 120 s e o teste falha por tempo, não por defeito.

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

### A legenda não aparece no vídeo

Por ordem de probabilidade:

1. **Faltam fontes** (ver [5.5](#55-uma-fonte-de-surpresas-as-fontes)). Sintoma: imagem certa, zero texto, nenhum erro.
2. `include_captions` foi enviado como `false`, ou `subtitle_style` tem `"mode": "hidden"`.
3. O `.srt` está vazio ou tem uma única cena.

### As legendas não batem com a narração

Não há `faster-whisper` instalado. Verifique com `GET /api/tts/status`. **Atenção:** neste caso o servidor não dá erro nenhum — `pipeline.transcribe_audio_file` engole a excepção e escreve cinco frases fixas de 3 segundos cada (`pipeline.py:84-94`). A resposta é `200` e não existe campo de aviso. Para o distinguir, instale `faster-whisper` e repita.

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
