#!/usr/bin/env python3
"""Dark Studio - verificacao de ambiente (preflight).

Valida, antes de arrancar o servidor, tudo o que costuma partir numa maquina
nova: versao do Python, pacotes em falta, ffmpeg/ffprobe, node/npx, permissao
de escrita em storage/, presenca de .env e presenca de chaves de API.

Uso (a partir da raiz do projeto):

    python scripts/check_env.py            # relatorio
    python scripts/check_env.py --fix      # cria as pastas de storage em falta
    python scripts/check_env.py --strict   # avisos tambem falham (exit 1)
    python scripts/check_env.py --import-check   # importa os pacotes (lento)

Codigo de saida: 0 se todos os requisitos obrigatorios passam, 1 caso contrario.
As chaves de API nunca sao impressas: so interessa saber se estao presentes.

Este ficheiro e deliberadamente ASCII-only: uma consola Windows em cp850
rebenta com um UnicodeEncodeError assim que se escreve um acento, e assim o
verificador morria com a unica coisa que devia diagnosticar.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

MIN_PYTHON = (3, 10)

ROOT = Path(__file__).resolve().parents[1]

# (modulo a importar, nome no pip, obrigatorio?, nota)
PACKAGES = (
    ("fastapi", "fastapi", True, "servidor web"),
    ("uvicorn", "uvicorn", True, "servidor ASGI"),
    ("pydantic", "pydantic", True, "validacao de dados"),
    ("httpx", "httpx", True, "cliente HTTP"),
    ("dotenv", "python-dotenv", True, "leitura do .env"),
    ("multipart", "python-multipart", True, "uploads de ficheiros"),
    ("numpy", "numpy", True, "analise numerica (viral_pipeline)"),
    ("librosa", "librosa", True, "analise de audio (obrigatorio: import no topo do modulo)"),
    ("edge_tts", "edge-tts", True, "narracao por TTS"),
    ("faster_whisper", "faster-whisper", False, "transcricao de audio, download grande"),
)

TEST_PACKAGES = (
    ("pytest", "pytest", "suite de testes"),
    ("pytest_asyncio", "pytest-asyncio", "asyncio_mode=auto no pytest.ini"),
    ("requests", "requests", "tests/test_e2e.py e test_live_providers.py"),
)

# (executavel, obrigatorio?, para que serve, como instalar no Windows)
BINARIES = (
    ("ffmpeg", False, "analise de video", "ffmpeg"),
    ("ffprobe", False, "duracao/resolucao de media", "ffmpeg"),
    ("node", False, "runtime do render HyperFrames", "node"),
    ("npx", False, "executa o HyperFrames", "node"),
)

# Nome das variaveis de ambiente. So interessa PRESENTE / AUSENTE.
API_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "YOUTUBE_API_KEY",
    "AZURE_SPEECH_KEY",
    "AZURE_SPEECH_REGION",
)

STORAGE_SUBDIRS = ("media", "outputs", "uploads", "thumbnails", "music")

OK = "OK"
FALTA = "FALTA"
AVISO = "AVISO"
INFO = "--"


class Report:
    """Acumula linhas da tabela e o estado global."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def add(self, label: str, status: str, detail: str = "") -> None:
        self.rows.append((label, status, detail))
        if status == FALTA:
            self.failures.append(label)
        elif status == AVISO:
            self.warnings.append(label)


def _package_version(pip_name: str) -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return "?"
    try:
        return version(pip_name)
    except PackageNotFoundError:
        return "?"
    except Exception:  # pragma: no cover - metadata corrompido
        return "?"


def _module_present(module: str, do_import: bool) -> bool:
    if do_import:
        try:
            __import__(module)
            return True
        except Exception:
            return False
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def _binary_version(path: str) -> str:
    flag = "--version"
    try:
        proc = subprocess.run(
            [path, flag],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return ""
    out = (proc.stdout or proc.stderr or "").strip()
    return out.splitlines()[0][:60] if out else ""


def _parse_env_file(path: Path) -> dict[str, str]:
    """Le o .env sem dependencias e sem imprimir valores."""
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _storage_status(fix: bool) -> tuple[str, str]:
    storage = ROOT / "storage"
    if not storage.exists():
        if not fix:
            return FALTA, "pasta storage/ ausente (crie com --fix)"
        try:
            storage.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return FALTA, "nao foi possivel criar storage/ (%s)" % (exc.strerror or exc)
    for name in STORAGE_SUBDIRS:
        sub = storage / name
        if sub.is_dir():
            continue
        if not fix:
            return FALTA, "faltam subpastas de storage/ (crie com --fix)"
        try:
            sub.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return FALTA, "nao foi possivel criar storage/%s (%s)" % (name, exc.strerror or exc)
    try:
        with tempfile.NamedTemporaryFile(prefix=".check_env_", dir=str(storage), delete=True):
            pass
    except OSError as exc:
        return FALTA, "sem permissao de escrita (%s)" % (exc.strerror or exc)
    return OK, "gravavel"


def build_report(fix: bool = False, do_import: bool = False) -> Report:
    rep = Report()

    version = sys.version_info
    py_txt = "%d.%d.%d" % version[:3]
    if version[:2] >= MIN_PYTHON:
        rep.add("Python >= %d.%d" % MIN_PYTHON, OK, py_txt)
    else:
        rep.add("Python >= %d.%d" % MIN_PYTHON, FALTA, "%s - instale Python 3.10 ou superior" % py_txt)
    rep.add("Interpretador", INFO, sys.executable)
    if getattr(sys, "base_prefix", sys.prefix) != sys.prefix:
        rep.add("Ambiente virtual", OK, sys.prefix)
    else:
        rep.add("Ambiente virtual", AVISO, "Python global - instale dentro de .venv")

    for module, pip_name, required, note in PACKAGES:
        label = "pacote %s" % pip_name
        if _module_present(module, do_import):
            rep.add(label, OK, _package_version(pip_name))
        elif required:
            rep.add(label, FALTA, "em falta - %s" % note)
        else:
            rep.add(label, AVISO, "em falta (opcional) - %s" % note)

    test_missing = [p for p in TEST_PACKAGES if not _module_present(p[0], do_import)]
    if test_missing:
        rep.add("Pacotes de teste", AVISO, "faltam: %s" % ", ".join(p[1] for p in test_missing))
    else:
        rep.add("Pacotes de teste", OK, "pytest, pytest-asyncio, requests")

    for name, required, why, tool in BINARIES:
        path = shutil.which(name)
        if path:
            # npx e um .cmd no Windows: nao se lanca em subprocess aqui, mostra so o caminho.
            detail = path if name == "npx" else _binary_version(path)
            rep.add(name, OK, detail or path)
        else:
            hint = _install_hint(tool)
            if required:
                rep.add(name, FALTA, "em falta - %s | %s" % (why, hint))
            else:
                rep.add(name, AVISO, "em falta - %s | %s" % (why, hint))

    status, detail = _storage_status(fix)
    rep.add("storage/", status, detail)

    env_path = ROOT / ".env"
    env_values = _parse_env_file(env_path)
    if env_path.is_file():
        rep.add(".env", OK, "%d variavel(is) definidas" % len(env_values))
    elif (ROOT / ".env.example").is_file():
        rep.add(".env", AVISO, "ausente - copie .env.example para .env")
    else:
        rep.add(".env", AVISO, "ausente - pode escrever as chaves em /api/settings")

    present = [k for k in API_KEYS if (env_values.get(k) or os.environ.get(k, "")).strip()]
    for key in API_KEYS:
        rep.add("chave %s" % key, OK if key in present else AVISO, "PRESENTE" if key in present else "ausente")
    if "OPENROUTER_API_KEY" not in present:
        rep.warnings.append("chave OPENROUTER_API_KEY")

    return rep


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True se podemos vincular a porta (nenhum processo a escutar nela).

    Deliberadamente SEM SO_REUSEADDR: queremos detetar o servidor a correr, nao
    apenas um socket em TIME_WAIT. Uma porta em TIME_WAIT eignorada e o efeito
    pretendido - o launchador salta para a seguinte.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except (OSError, OverflowError, ValueError):
            return False
    return True


def pick_port(base: int, maximum: int, host: str = "127.0.0.1") -> int | None:
    """Primeira porta livre de `base` a `maximum` (inclusive)."""
    if base > maximum:
        maximum = base
    for port in range(max(1, base), min(maximum, 65535) + 1):
        if port_is_free(port, host):
            return port
    return None


def _port_env_int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _run_port_mode(args: argparse.Namespace) -> int:
    """Modo --check-port / --pick-port: usado pelos launchers, sem relatorio."""
    host = args.host
    if args.check_port is not None:
        if not 1 <= args.check_port <= 65535:
            print("porta invalida: %d" % args.check_port, file=sys.stderr)
            return 1
        free = port_is_free(args.check_port, host)
        print("free" if free else "busy")
        return 0 if free else 1
    base = args.pick_port[0] if args.pick_port and args.pick_port[0] is not None else _port_env_int("DARK_STUDIO_PORT", 8013)
    if args.pick_port and len(args.pick_port) > 1 and args.pick_port[1] is not None:
        maximum = args.pick_port[1]
    else:
        maximum = _port_env_int("DARK_STUDIO_PORT_MAX", base + 20)
    if not 1 <= base <= 65535:
        print("porta base invalida: %d" % base, file=sys.stderr)
        return 1
    port = pick_port(base, maximum, host)
    if port is None:
        print("nenhuma porta livre entre %d e %d" % (base, maximum), file=sys.stderr)
        return 1
    # --machine existe para os launchers: o cmd.exe e o sh fazem parse desta
    # linha com tokens, assim um aviso unexpectedo do Python nao se confused
    # com a porta.
    print("PORT=%d" % port if args.machine else port)
    return 0


def _install_hint(tool: str) -> str:
    """Como instalar um binario externo no sistema operativo em uso."""
    if os.name == "nt":
        return {
            "ffmpeg": "winget install --id Gyan.FFmpeg -e  (ou: choco install ffmpeg)",
            "node": "instalar o Node.js LTS: https://nodejs.org/en/download",
        }.get(tool, tool)
    return {
        "ffmpeg": "sudo apt install ffmpeg  (ou: brew install ffmpeg)",
        "node": "instalar o Node.js LTS pelo gestor de pacotes da distro",
    }.get(tool, tool)


def _missing_binaries() -> list[tuple[str, str, str, str]]:
    return [(name, req, why, tool) for name, req, why, tool in BINARIES if not shutil.which(name)]


def _install_hints() -> str:
    """Bloco de texto com os comandos para os binarios em falta (sem repetir)."""
    lines: list[str] = []
    seen: set[str] = set()
    for _name, _req, _why, tool in _missing_binaries():
        if tool in seen:
            continue
        seen.add(tool)
        lines.append(_install_hint(tool))
    return "".join("    %s\n" % line for line in lines)


def render(rep: Report) -> None:
    label_w = max(len(r[0]) for r in rep.rows)
    status_w = max(len(r[1]) for r in rep.rows)
    detail_w = max(len(r[2]) for r in rep.rows)
    width = label_w + status_w + detail_w + 6
    header = "  %-*s  %-*s  %s" % (label_w, "VERIFICACAO", status_w, "ESTADO", "DETALHE")
    print("")
    print("  " + "=" * width)
    print(header)
    print("  " + "-" * width)
    for label, status, detail in rep.rows:
        print("  %-*s  %-*s  %s" % (label_w, label, status_w, status, detail))
    print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_env.py",
        description="Verificacao de ambiente do Dark Studio.",
    )
    parser.add_argument("--fix", action="store_true", help="criar as pastas de storage/ em falta")
    parser.add_argument("--strict", action="store_true", help="considerar avisos como falha")
    parser.add_argument(
        "--import-check",
        action="store_true",
        help="importar os pacotes em vez de os procurar (lento: librosa demora)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="host usado na sonda de portas")
    parser.add_argument(
        "--machine",
        action="store_true",
        help="saida em pares CHAVE=valor, facil de ler do cmd.exe e do sh",
    )
    parser.add_argument(
        "--check-port",
        type=int,
        metavar="N",
        help="sonda uma porta: imprime free/busy e sai com 0/1 (usado pelos launchers)",
    )
    parser.add_argument(
        "--pick-port",
        type=int,
        nargs="*",
        metavar="N",
        help="imprime a primeira porta livre a partir de N (usado pelos launchers)",
    )
    args = parser.parse_args(argv)

    if args.check_port is not None or args.pick_port is not None:
        return _run_port_mode(args)

    print("")
    print("  DARK STUDIO - verificacao de ambiente")
    print("  Projeto: %s" % ROOT)
    if args.import_check:
        print("  (--import-check: a importar pacotes, pode demorar ~10s por causa do librosa)")

    rep = build_report(fix=args.fix, do_import=args.import_check)
    render(rep)

    checked = len(rep.rows)
    print("  Verificacoes: %d | falhas: %d | avisos: %d" % (checked, len(rep.failures), len(rep.warnings)))
    if rep.warnings:
        print("  Avisos: %s" % ", ".join(rep.warnings))
    print("")

    if rep.failures:
        print("  RESULTADO: FALHOU - %s" % ", ".join(rep.failures))
        print("  Corrija assim:")
        is_windows = os.name == "nt"
        print("    python -m venv .venv")
        print("    .venv\\Scripts\\python -m pip install -r requirements.txt" if is_windows
              else "    .venv/bin/python -m pip install -r requirements.txt")
        print(_install_hints())
        print("")
        return 1

    if rep.warnings and args.strict:
        print("  RESULTADO: FALHOU (--strict: ha avisos)")
        print("")
        return 1

    print("  RESULTADO: OK - pode arrancar com start.bat (Windows) ou sh start.sh")
    if rep.warnings:
        print("  O servidor arranca, mas as funcionalidades em falta falham a meio.")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
