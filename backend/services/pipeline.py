from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any
import hashlib
import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from backend.services.auth_contract import (
    AUTH_INVALID_KEY,
    AUTH_MISSING_KEY,
    AUTH_MODEL_NOT_FREE,
    AUTH_QUOTA_EXCEEDED,
    AUTH_RATE_LIMIT,
    AUTH_REQUEST_FAILED,
    AuthError,
    provider_status_error,
)
from backend.services.provider_registry import (
    get_registry,
    call_free_model as registry_call_free_model,
    get_provider_status as registry_get_provider_status,
    ProviderType,
    _OPENROUTER_QUOTA,
)

BASE_DIR = Path(__file__).resolve().parents[2]
# override=True: the .env file is the project-scoped config the settings UI writes
# to. Without it, a stale ambient OS variable of the same name silently wins and
# saving a new key from the UI appears to do nothing.
load_dotenv(BASE_DIR / ".env", override=True)
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


def parse_srt_timestamp(value: str) -> float:
    h, m, s_ms = value.replace(',', '.').split(':')
    s, ms = s_ms.split('.') if '.' in s_ms else (s_ms, '0')
    return int(h) * 3600 + int(m) * 60 + float(s) + float(f"0.{ms}" if ms else 0)


def format_timestamp(milliseconds: int) -> str:
    total_seconds = milliseconds / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    secs = int(total_seconds % 60)
    millis = int(round((total_seconds - int(total_seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def transcribe_audio_file(file_path: Path) -> List[Dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(file_path), language="pt", beam_size=5)

        results = []
        for seg in segments:
            results.append({
                "index": len(results) + 1,
                "start": float(seg.start),
                "end": float(seg.end),
                "text": str(seg.text).strip(),
            })
        if results:
            return results
    except Exception:
        pass

    phrases = [
        "A rotina moderna nos consome em excesso.",
        "As pessoas passam o dia correndo contra o tempo.",
        "O corpo humano foi feito para se mover.",
        "A atividade física melhora a saúde mental.",
        "A consistência é mais importante do que intensidade.",
    ]
    return [
        {"index": i + 1, "start": i * 3.0, "end": (i + 1) * 3.0, "text": phrase}
        for i, phrase in enumerate(phrases)
    ]


def build_srt_from_segments(segments: List[Dict[str, Any]]) -> str:
    lines = []
    for seg in segments:
        start = format_timestamp(int(seg["start"] * 1000))
        end = format_timestamp(int(seg["end"] * 1000))
        lines.append(str(seg["index"]))
        lines.append(f"{start} --> {end}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# Words that appear constantly in narration but tell a stock-photo search nothing.
# Searching "trabalha" returns offices; searching "papeis" returns banknotes.
STOPWORDS = {
    # articles, pronouns, prepositions, conjunctions
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "do", "da", "dos", "das",
    "em", "no", "na", "nos", "nas", "por", "pelo", "pela", "pelos", "pelas", "para",
    "com", "sem", "sob", "sobre", "entre", "ate", "ate", "desde", "apos", "e", "ou",
    "mas", "porem", "porque", "que", "se", "como", "quando", "onde", "qual", "quais",
    "este", "esta", "isso", "isto", "esse", "essa", "aquele", "aquela", "aquilo",
    "meu", "minha", "teu", "tua", "nos", "vos", "lhe", "eles", "elas", "ele", "ela",
    "eu", "tu", "voce", "eles", "nos", "vos", "me", "te", "se", "lhes", "ainda",
    # very common verbs and their forms
    "ser", "sera", "sou", "es", "e", "esta", "estao", "estar", "foi", "for",
    "ter", "tem", "tinha", "teve", "haver", "ha", "havia", "faz", "fazer", "fez",
    "pode", "podem", "poder", "deve", "devem", "dever", "quer", "querem", "querer",
    "vai", "vao", "ir", "va", "vem", "vir", "vem", "diz", "dizer", "disse", "dizem",
    "sabe", "saber", "sabia", "acha", "achar", "acham", "olha", "ver", "ve", "veem",
    "sabe", "conhece", "conhecer", "trabalha", "trabalhar", "trabalham", "trabalho",
    "continua", "continuar", "continuara", "ensina", "ensinar", "ensinaram",
    "explica", "explicar", "explicacao", "diz", "dizer", "dizendo",
    "mexe", "mexer", "mexendo", "troca", "trocar", "trocando", "troco",
    "acumula", "acumular", "acumulando", "acumulado", "guarda", "guardar",
    "investe", "investir", "investimento", "gasta", "gastar", "gasto",
    # adverbs and time/filler words
    "muito", "mais", "menos", "pouco", "bastante", "muito", "sempre", "nunca",
    "agora", "hoje", "ontem", "amanha", "dia", "dias", "noite", "manha", "tarde",
    "vez", "vezes", "ano", "anos", "mes", "meses", "semana", "semanas", "hora",
    "horas", "minuto", "minutos", "dia", "tempo", "parte", "partes", "caso", "casos",
    "momento", "momentos", "maneira", "forma", "lugar", "coisa", "coisas",
    "grande", "grande", "pequeno", "pequena", "primeiro", "primeira", "ultimo",
    "proximo", "proxima", "unico", "unica", "todo", "toda", "todos", "todas",
    "outro", "outra", "outros", "outras", "mesmo", "mesma", "tao", "bem", "muito",
    "sim", "nao", "nunca", "sempre", "quase", "cerca", "logo", "entao", "portanto",
    "porque", "enquanto", "durante", "sobre", "contra", "entre", "atraves",
}

# SRT structure that must never become a search term.
_SRT_NOISE = re.compile(r"^\d+$|-->|\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3}")
_WORD_SPLIT = re.compile(r"[^\wÀ-ÿ]+", re.UNICODE)

# Long words are not automatically good search terms: an adverb or a conjugated verb
# is long and vague. Penalise the inflections that show up most in narration.
_VAGUE_SUFFIXES = (
    # adverbs
    "mente",
    # gerunds and participles, accented and not
    "ando", "endo", "indo", "ando", "endo",
    # future / conditional endings, accented and not
    "ara", "erá", "ira", "irá", "ariam", "ariam", "ariam", "ariam",
    "ava", "avam", "aram", "eriam", "eriam",
    # past participles
    "ado", "ida", "ido", "ados", "idas", "idos",
    # abstract nouns that rarely match a photograph
    "acao", "ações", "icao", "ições", "ancia", "ância", "encia", "ência",
    "ismo", "logia", "grafia", "metria",
    # degree adjectives
    "avel", "ível", "ivel", "ivel",
)


def _vagueness_penalty(word: str) -> float:
    for suffix in _VAGUE_SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix) + 3:
            return 6.0
    return 0.0


def extract_keywords_from_text(text: str, limit: int = 8, max_phrases: int = 2) -> List[str]:
    """Content words worth sending to a stock-photo search.

    Drops SRT structure and stopwords, penalises vague inflections, and interleaves
    two-word phrases with single words: stock search matches "papéis coloridos" far
    better than the bare adjective, but a phrase-only list loses strong topic words
    like "dinheiro".
    """
    if not text or not text.strip():
        return []

    unigrams: Dict[str, float] = {}
    phrases: Dict[str, float] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _SRT_NOISE.match(line):
            continue

        line_tokens: List[str] = []
        for token in _WORD_SPLIT.split(line.lower()):
            token = token.strip()
            if len(token) < 4 or token in STOPWORDS or token.isdigit():
                continue
            line_tokens.append(token)
            unigrams[token] = unigrams.get(token, 0.0) + len(token) - _vagueness_penalty(token)

        # Non-overlapping pairs only: sliding windows invent junk like
        # "matelas celular" out of unrelated phrases.
        for first, second in zip(line_tokens[::2], line_tokens[1::2]):
            phrase = f"{first} {second}"
            phrases[phrase] = phrases.get(phrase, 0.0) + len(phrase) + 3

    ranked_words = sorted(unigrams.items(), key=lambda item: (-item[1], item[0]))
    ranked_phrases = sorted(phrases.items(), key=lambda item: (-item[1], item[0]))
    top_phrases = [term for term, _ in ranked_phrases[:max_phrases]]
    top_words = [term for term, _ in ranked_words]

    # Interleave so a short limit still yields both a phrase and a strong word.
    picked: List[str] = []
    word_iter = iter(top_words)
    for phrase in top_phrases:
        picked.append(phrase)
        word = next(word_iter, None)
        if word is not None and len(picked) < limit:
            picked.append(word)
    picked.extend(term for term in word_iter if len(picked) < limit)

    return [term for term in picked if term not in STOPWORDS][:limit]


def extract_scene_keywords(storyboard: List[Dict[str, Any]], limit: int = 3) -> Dict[int, List[str]]:
    """Search terms per scene, drawn from that scene's own caption.

    A single global keyword list gives every scene the same stock imagery. Each
    scene describing its own subject is what makes the visuals track the narration.
    """
    per_scene: Dict[int, List[str]] = {}
    for idx, scene in enumerate(storyboard):
        caption = str(scene.get("caption") or scene.get("headline") or "")
        per_scene[idx] = extract_keywords_from_text(caption, limit=limit)
    return per_scene


DEFAULT_FREE_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"


def get_provider_status() -> Dict[str, Any]:
    """Delegates to the provider registry for unified multi-provider status."""
    return registry_get_provider_status()


def extract_json_payload(text: str) -> Any:
    """Pull the first balanced JSON value out of a model response.

    Free reasoning models prepend a "Here's a thinking process:" preamble even when
    asked not to, so a bare json.loads is not enough. Scans for the first balanced
    object/array while respecting strings and escapes.
    """
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(cleaned)):
                char = cleaned[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cleaned[start:index + 1])
                        except json.JSONDecodeError:
                            break
            start = cleaned.find(opener, start + 1)

    raise ValueError("Nenhum JSON válido na resposta do modelo.")


def call_free_model(prompt: str, *, json_mode: bool = False, max_tokens: int = 400,
                    model: Optional[str] = None, provider: Optional[str] = None) -> str:
    """Call the configured free model via provider registry.

    Delegates to the registry's call_free_model. Maintains backward compatibility.
    """
    return registry_call_free_model(
        prompt,
        json_mode=json_mode,
        max_tokens=max_tokens,
        model=model,
        provider=provider,
        prefer_free=True,
    )


MIN_SCENE_DURATION = 0.8
# How much consecutive scenes overlap so a cross-transition (wipe) has something
# to reveal over. Media lives on track 0 and may overlap; captions are trimmed so
# two are never on screen at once.
SCENE_OVERLAP = 0.7


def normalize_scene_timings(
    storyboard: List[Dict[str, Any]],
    min_duration: float = MIN_SCENE_DURATION,
    overlap: float = SCENE_OVERLAP,
) -> List[Dict[str, Any]]:
    """Lay scenes out so a cross-transition works without stacking captions.

    Every scene after the first starts ``overlap`` seconds earlier than its source
    time, so the incoming wipe always has the outgoing scene behind it. Ends stay on
    the source timeline, which keeps the original narration in sync — gaps between
    segments are covered visually by the incoming scene while the audio is silent,
    which reads as a tight edit rather than dead air.

    Captions are trimmed to the non-overlapped part of their scene by the
    composition generator, which is where the neighbour's start is known. A segment
    shorter than ``min_duration`` is stretched to it.
    """
    normalised: List[Dict[str, Any]] = []
    last_index = len(storyboard) - 1

    for idx, scene in enumerate(storyboard):
        item = dict(scene)
        source_start = max(0.0, float(item.get("start", 0.0)))
        source_end = float(item.get("end", source_start))
        if source_end - source_start < min_duration:
            source_end = source_start + min_duration

        start = 0.0 if idx == 0 else max(0.0, source_start - overlap)
        # Degenerate input (two scenes at the same source time) must not stack.
        if normalised and start <= normalised[-1]["start"]:
            start = normalised[-1]["start"] + 0.2

        end = source_end if idx == last_index else max(start + 0.4, source_end)
        if end - start < min_duration and idx == last_index:
            end = start + min_duration

        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        item["duration"] = round(end - start, 3)
        item["source_start"] = round(source_start, 3)
        item["source_end"] = round(source_end, 3)
        normalised.append(item)

    return normalised


def build_storyboard_from_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    palette = ["#0f172a", "#1d4ed8", "#10b981", "#f59e0b", "#ef4444", "#a855f7"]
    scenes: List[Dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        headline = text[:80].strip()
        short_text = text if len(text) <= 80 else text[:77].rstrip() + "..."
        scenes.append({
            "index": int(segment.get("index", len(scenes) + 1)),
            "start": start,
            "end": end,
            "duration": max(0.0, end - start),
            "headline": headline,
            "caption": short_text,
            "background": palette[(len(scenes) + 1) % len(palette)],
            "tone": "narrative" if len(scenes) % 2 == 0 else "motivation",
            "media_url": "",
            "transition": "fade" if len(scenes) % 2 == 0 else "slide_left",
            "effect": "cinematic" if len(scenes) % 2 == 0 else "glow",
            "caption_style": "bottom" if len(scenes) % 2 == 0 else "center",
        })
    return normalize_scene_timings(scenes)


def build_edit_plan_from_segments(segments: List[Dict[str, Any]], media: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    storyboard = build_storyboard_from_segments(segments)
    plan: List[Dict[str, Any]] = []
    for idx, scene in enumerate(storyboard):
        media_item = media[idx % len(media)] if media else {}
        plan.append({
            "scene_index": scene["index"],
            "start": scene["start"],
            "end": scene["end"],
            "duration": scene["duration"],
            "transition": scene.get("transition", "fade"),
            "effect": scene.get("effect", "cinematic"),
            "caption_style": scene.get("caption_style", "bottom"),
            "headline": scene["headline"],
            "media_url": media_item.get("url", scene.get("media_url", "")),
            "motion": "zoom_in" if idx % 2 == 0 else "pan_left",
        })
    return plan


def parse_srt_to_segments(srt_path: Path) -> List[Dict[str, Any]]:
    if not srt_path.exists():
        return []
    try:
        srt_text = srt_path.read_text(encoding="utf-8")
    except Exception:
        return []

    segments: List[Dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        try:
            start_text, end_text = lines[1].split("-->", 1)
            start = parse_srt_timestamp(start_text.strip())
            end = parse_srt_timestamp(end_text.strip())
        except Exception:
            continue

        text = " ".join(lines[2:])
        segments.append({
            "index": len(segments) + 1,
            "start": start,
            "end": end,
            "text": text,
        })
    return segments


_MEDIA_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def search_media_for_keywords(keywords: List[str]) -> List[Dict[str, Any]]:
    """Search real stock media for each keyword across every enabled provider.

    Results are cached per keyword so repeated builds do not re-hit the APIs.
    Returns an empty list when no provider is configured - never fabricated data.
    """
    provider_status = get_provider_status()
    enabled = [name for name in ("pexels", "pixabay") if provider_status.get(name, {}).get("enabled")]

    if not keywords or not enabled:
        return []

    result: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    for keyword in keywords[:4]:
        cached = _MEDIA_CACHE.get(keyword)
        if cached is None:
            found: List[Dict[str, Any]] = []
            for provider in enabled:
                # AuthError propagates on purpose: a bad key must not be cached
                # as "this keyword has no media".
                found.extend(fetch_provider_media(keyword, provider))
            _MEDIA_CACHE[keyword] = found
            cached = found

        for item in cached:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            result.append({**item, "keyword": keyword})

    return result


def extract_visual_terms_with_ai(
    storyboard: List[Dict[str, Any]],
    niche: str = "",
) -> Optional[Dict[int, List[str]]]:
    """Ask the model for stock-search terms per scene.

    Two reasons this beats keyword extraction from the caption: the model can name
    what a scene should *look like* rather than what it says, and stock providers
    match English far better than Portuguese ("banknotes", not "papéis coloridos").

    Returns None when the provider is unavailable or the quota is spent, so the
    caller can fall back to the heuristic.
    """
    status = get_provider_status()["openrouter"]
    if not status["enabled"] or status.get("quota_exhausted"):
        return None
    if not storyboard:
        return None

    lines = []
    for idx, scene in enumerate(storyboard):
        caption = str(scene.get("caption") or scene.get("headline") or "")[:220]
        lines.append(f"{idx}: {caption}")

    prompt = (
        "You are art-directing a stock-footage edit.\n"
        f"Video topic: {niche or 'general'}\n\n"
        "Scenes (index: narration):\n" + "\n".join(lines) + "\n\n"
        "For each scene give 2 short search terms in ENGLISH naming what should be "
        "seen on screen. Use concrete photographic subjects, not the narration's "
        "abstract words. Never repeat a term across scenes.\n"
        'Answer only JSON: {"terms": {"0": ["banknotes", "currency"], "1": [...]}}\n'
        "Keys are scene indexes as strings."
    )

    try:
        raw = call_free_model(prompt, json_mode=True, max_tokens=700)
        parsed = extract_json_payload(raw)
    except Exception:
        # Deliberately broad: the caller has a heuristic fallback, so any provider
        # hiccup should degrade the search terms, not fail the whole build.
        return None

    if not isinstance(parsed, dict):
        return None
    terms_map = parsed.get("terms", parsed)
    if not isinstance(terms_map, dict):
        return None

    result: Dict[int, List[str]] = {}
    for key, value in terms_map.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if cleaned:
            result[idx] = cleaned[:3]

    return result or None


def search_media_for_scenes(
    storyboard: List[Dict[str, Any]],
    limit: int = 3,
    niche: str = "",
) -> tuple[Dict[int, List[Dict[str, Any]]], str]:
    """Search stock media per scene, using that scene's own subject.

    Prefers model-authored English terms and falls back to keywords taken from the
    caption. Returns ``({scene_index: [media, ...]}, term_source)`` where the source
    is ``"ai"`` or ``"heuristic"``. A scene with nothing to search for gets an empty
    list rather than borrowing an unrelated scene's imagery.
    """
    provider_status = get_provider_status()
    enabled = [name for name in ("pexels", "pixabay") if provider_status.get(name, {}).get("enabled")]
    if not enabled:
        return {}, "none"

    ai_terms = extract_visual_terms_with_ai(storyboard, niche=niche)
    heuristic_terms = extract_scene_keywords(storyboard, limit=limit)
    source = "ai" if ai_terms else "heuristic"

    per_scene: Dict[int, List[Dict[str, Any]]] = {}
    for idx in range(len(storyboard)):
        terms = (ai_terms or {}).get(idx) or heuristic_terms.get(idx) or []
        if not terms:
            per_scene[idx] = []
            continue

        found: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for keyword in terms[:2]:
            for item in search_media_for_keywords([keyword]):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                found.append(item)
            if len(found) >= 4:
                break
        per_scene[idx] = found

    return per_scene, source


def fetch_provider_media(query: str, provider: str = "pexels") -> List[Dict[str, Any]]:
    provider_status = get_provider_status()
    if not provider_status.get(provider, {}).get("enabled"):
        return []
    if not query.strip():
        return []

    try:
        headers = {"Accept": "application/json"}
        if provider == "pixabay":
            url = "https://pixabay.com/api/"
            params = {"key": os.getenv("PIXABAY_API_KEY"), "q": query, "per_page": 3}
        elif provider == "pexels":
            url = "https://api.pexels.com/v1/search"
            params = {"query": query, "per_page": 3}
            headers["Authorization"] = os.getenv("PEXELS_API_KEY", "")
        else:
            return []

        response = httpx.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        if provider == "pixabay":
            hits = data.get("hits", [])
            results = []
            for hit in hits[:3]:
                # largeImageURL keeps full resolution; webformatURL is the small variant.
                media_url = hit.get("largeImageURL") or hit.get("webformatURL") or ""
                if not media_url:
                    continue
                results.append({
                    "source": "Pixabay",
                    "provider": "pixabay",
                    "title": str(hit.get("tags") or query).split(",")[0].strip(),
                    "url": media_url,
                    "kind": "video" if hit.get("type") == "video" else "image",
                    "width": hit.get("imageWidth"),
                    "height": hit.get("imageHeight"),
                })
            return results

        if provider == "pexels":
            hits = data.get("photos", [])
            results = []
            for hit in hits[:3]:
                src = hit.get("src", {})
                media_url = src.get("large2x") or src.get("original") or src.get("large") or ""
                if not media_url:
                    continue
                results.append({
                    "source": "Pexels",
                    "provider": "pexels",
                    "title": query,
                    "url": media_url,
                    "kind": "image",
                    "width": hit.get("width"),
                    "height": hit.get("height"),
                })
            return results
    except httpx.HTTPStatusError as exc:
        # A rejected or throttled key must surface. Returning [] here would look
        # like "this search simply has no results" and hide a broken credential.
        status = exc.response.status_code
        if status in (401, 402, 403, 429) or status >= 500:
            raise provider_status_error(provider, status, exc.response.text) from exc
        return []
    except httpx.HTTPError:
        return []
    except (KeyError, TypeError, ValueError):
        return []

    return []


def download_media_asset(media_url: str, project_name: str, scene_index: int) -> Path | None:
    if not media_url:
        return None
    try:
        parsed = urlparse(media_url)
        suffix = Path(parsed.path).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            suffix = ".jpg"
        target_dir = STORAGE_DIR / "media" / project_name
        target_dir.mkdir(parents=True, exist_ok=True)
        # Key the cache by URL as well as scene index. Keying on the index alone
        # meant a second candidate for the same scene returned the first
        # candidate's file, so quality-based substitution could never make
        # progress, and a re-run silently reused last run's asset.
        fingerprint = hashlib.sha1(media_url.encode("utf-8")).hexdigest()[:10]
        target_path = target_dir / f"scene_{scene_index}_{fingerprint}{suffix}"
        if target_path.exists():
            return target_path

        if parsed.scheme in ("http", "https"):
            response = httpx.get(media_url, timeout=20, follow_redirects=True)
            response.raise_for_status()
            payload = response.content
        else:
            # A local path: already on disk, nothing to fetch.
            source = Path(media_url)
            if not source.exists():
                return None
            payload = source.read_bytes()

        if len(payload) < 1024:
            return None
        target_path.write_bytes(payload)
        return target_path
    except Exception:
        return None


def get_media_duration(media_path: Path) -> float | None:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(media_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(probe.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def fit_storyboard_to_duration(storyboard: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    if duration <= 0 or not storyboard:
        return storyboard
    fitted: List[Dict[str, Any]] = []
    cursor = 0.0
    for scene in storyboard:
        start = max(cursor, float(scene.get("start", cursor)))
        end = min(duration, max(start, float(scene.get("end", start + 3.0))))
        if end <= start:
            continue
        item = {**scene, "start": start, "end": end, "duration": end - start}
        fitted.append(item)
        cursor = end
        if cursor >= duration:
            break
    if fitted and fitted[-1]["end"] < duration:
        fitted[-1]["end"] = duration
        fitted[-1]["duration"] = duration - fitted[-1]["start"]
    return fitted


