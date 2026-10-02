"""Turn a topic into a finished narration script.

This is the first stage of the one-click pipeline: everything downstream (TTS,
stock search, edit plan) needs narration text that already exists, so this module
is what removes the "upload an audio file" prerequisite.

The free model is used opportunistically and never depended upon. Free models
are flaky by nature (rate limits, daily quota, reasoning preambles, truncated
JSON), so a model failure degrades to a locally generated template script
instead of failing the run - the same contract ``/api/strategy`` already uses.

Only free (``:free``) OpenRouter models are ever called: there is deliberately no
parameter through which a caller could name a model, matching the ``/api/settings``
invariant that rejects anything not ending in ``:free``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from backend.services import pipeline
from backend.services.pipeline import BASE_DIR, STOPWORDS

# override=True, same reason as pipeline.py: the .env file is the project-scoped
# config the settings UI writes to, and must win over a stale ambient variable.
load_dotenv(BASE_DIR / ".env", override=True)

LANGUAGES: Dict[str, str] = {
    "pt-PT": "Portuguese (Portugal)",
    "pt-BR": "Portuguese (Brazil)",
    "en-US": "English (United States)",
    "es-ES": "Spanish (Spain)",
    "fr-FR": "French (France)",
}
DEFAULT_LANGUAGE = "pt-PT"

MAX_SECTIONS = 10
MIN_SECTIONS = 1
MIN_DURATION = 15
MAX_DURATION = 600

# Narration pace. 2.5 words/second is a normal documentary read, and it is what
# turns a word count into a duration the storyboard can be fitted to.
WORDS_PER_SECOND = 2.5

MAX_TOPIC_CHARS = 300
MAX_INSTRUCTIONS_CHARS = 600
MAX_TITLE_CHARS = 160
MAX_HOOK_CHARS = 400
MAX_SECTION_CHARS = 2000
MAX_TERM_CHARS = 60
MAX_TERMS_PER_SECTION = 3

SOURCE_MODEL = "openrouter-free"
SOURCE_FALLBACK = "fallback-local"


@dataclass
class ScriptSection:
    index: int
    text: str
    visual_terms: List[str] = field(default_factory=list)


@dataclass
class Script:
    title: str
    hook: str
    sections: List[ScriptSection]
    language: str
    tone: str
    source: str
    model: Optional[str] = None
    fallback_error: Optional[str] = None

    @property
    def full_text(self) -> str:
        """Every narration block joined by a blank line, ready for TTS."""
        return "\n\n".join(section.text for section in self.sections if section.text)

    @property
    def estimated_seconds(self) -> float:
        words = len(self.full_text.split())
        return round(words / WORDS_PER_SECOND, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "hook": self.hook,
            "sections": [
                {
                    "index": section.index,
                    "text": section.text,
                    "visual_terms": list(section.visual_terms),
                }
                for section in self.sections
            ],
            "language": self.language,
            "tone": self.tone,
            "source": self.source,
            "model": self.model,
            "fallback_error": self.fallback_error,
            "full_text": self.full_text,
            "estimated_seconds": self.estimated_seconds,
            "section_count": len(self.sections),
        }


def get_languages() -> List[Dict[str, str]]:
    """Language options for the UI dropdown."""
    return [{"code": code, "label": label} for code, label in LANGUAGES.items()]


def _clean_user_text(value: Any, limit: int) -> str:
    """Normalise user input before it reaches a prompt.

    Topic and instructions come straight from the UI, so newlines and control
    characters are flattened (a prompt-injecting line break is pointless once the
    text is on one line) and the length is bounded to protect the token budget.
    """
    text = "" if value is None else str(value)
    text = text.replace("\x00", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _current_model() -> Optional[str]:
    """Configured free model name, or None if the status cannot be read."""
    try:
        return str(pipeline.get_provider_status()["openrouter"]["model"])
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def _unavailable_reason() -> Optional[str]:
    """Why the free model cannot be called right now, or None if it can.

    Checked before the call so an unconfigured key or an exhausted daily quota
    skips the request entirely instead of paying for the failure path.
    """
    try:
        openrouter = pipeline.get_provider_status()["openrouter"]
    except (KeyError, TypeError, AttributeError, ValueError):
        return "estado do fornecedor indisponivel"
    if not openrouter.get("enabled"):
        return "nenhum fornecedor de modelo gratis esta configurado"
    if openrouter.get("quota_exhausted"):
        return "a quota diaria de modelos gratis esta esgotada"
    if not openrouter.get("free_only", True):
        return "o modelo configurado nao termina em :free"
    return None


def build_prompt(
    topic: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    section_count: int = 5,
    tone: str = "documentary",
    duration_target: int = 60,
    custom_instructions: str = "",
) -> str:
    """The exact text sent to the free model. Kept separate so it is inspectable."""
    label = LANGUAGES.get(language, LANGUAGES[DEFAULT_LANGUAGE])
    words_per_section = max(8, int(duration_target * WORDS_PER_SECOND / max(1, section_count)))
    instructions = _clean_user_text(custom_instructions, MAX_INSTRUCTIONS_CHARS)
    extra = (
        "\nAdditional requirements from the user "
        "(follow them unless they break the JSON format):\n"
        f"{instructions}\n"
        if instructions
        else ""
    )
    return (
        "You are a video scriptwriter for short documentary-style videos.\n"
        f"Topic: {topic}\n"
        f"Narration language: {label} ({language}). Write EVERY narration string in that language.\n"
        f"Tone: {tone}.\n"
        f"Target duration: about {duration_target} seconds, split into EXACTLY {section_count} sections.\n"
        f"Each section must be roughly {words_per_section} words of spoken prose.\n\n"
        "Answer ONLY with a single JSON object, no commentary, no markdown fence:\n"
        '{"title": string, "hook": string, "sections": [{"text": string, "visual_terms": [string, string]}]}\n'
        "\nRules:\n"
        f'- "sections" must contain EXACTLY {section_count} objects, in narrative order.\n'
        '- "title" is a short punchy video title in the narration language.\n'
        '- "hook" is the first spoken line: one sentence that makes a viewer stay.\n'
        '- "text" is plain narration prose. No markdown, no labels, no speaker names, no stage directions.\n'
        '- "visual_terms" is 2-3 short stock-footage search terms naming what is ON SCREEN for that section.\n'
        "  visual_terms MUST be in ENGLISH even when the narration is in another language: "
        "stock providers index English far better.\n"
        '  They must be concrete and photographic (for example "banknotes close up", '
        'not "money and the economy").\n'
        "  Never reuse the same term across sections.\n"
        "- Do not invent statistics, studies, dates or quotations presented as fact.\n"
        f"{extra}"
    )


def _parse_model_answer(
    raw: str,
    *,
    section_count: int,
    fallback_title: str,
    fallback_hook: str,
) -> Dict[str, Any]:
    """Validate the model answer, or raise ValueError explaining the rejection.

    Validation mirrors ``/api/strategy``: anything that would render the script
    unusable (missing fields, a short section list, malformed search terms, empty
    narration) is a hard rejection that routes to the local fallback rather than
    being patched up with placeholder data.
    """
    parsed = pipeline.extract_json_payload(raw)
    if not isinstance(parsed, dict):
        raise ValueError("o modelo nao devolveu um objecto JSON")

    required = {"title", "hook", "sections"}
    missing = required - {str(key).lower() for key in parsed}
    if missing:
        raise ValueError(f"campos obrigatorios faltando: {sorted(missing)}")
    lowered = {str(key).lower(): value for key, value in parsed.items()}

    sections = lowered.get("sections")
    if not isinstance(sections, list):
        raise ValueError("'sections' deve ser uma lista")
    if len(sections) < section_count:
        raise ValueError(f"'sections' tem {len(sections)} itens e sao necessarios {section_count}")

    cleaned: List[ScriptSection] = []
    for position, item in enumerate(sections[:section_count], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"a seccao {position} nao e um objecto JSON")
        fields = {str(key).lower(): value for key, value in item.items()}

        text = _clean_user_text(fields.get("text", ""), MAX_SECTION_CHARS)
        if not text:
            raise ValueError(f"o texto da seccao {position} esta vazio")

        terms = fields.get("visual_terms")
        if not isinstance(terms, list):
            raise ValueError(f"'visual_terms' da seccao {position} nao e uma lista")
        cleaned_terms: List[str] = []
        for term in terms:
            value = _clean_user_text(term, MAX_TERM_CHARS)
            if value and value not in cleaned_terms:
                cleaned_terms.append(value)
        if not cleaned_terms:
            raise ValueError(f"'visual_terms' da seccao {position} esta vazio")

        cleaned.append(
            ScriptSection(
                index=position,
                text=text,
                visual_terms=cleaned_terms[:MAX_TERMS_PER_SECTION],
            )
        )

    title = _clean_user_text(lowered.get("title", ""), MAX_TITLE_CHARS) or fallback_title
    hook = _clean_user_text(lowered.get("hook", ""), MAX_HOOK_CHARS) or fallback_hook
    return {"title": title, "hook": hook, "sections": cleaned}


_FALLBACK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "pt-PT": {
        "title": "{topic}: a história que quase ninguém viu",
        "hook": (
            "Em menos de um minuto, vais perceber porque {topic} mudou tudo, "
            "e quase ninguém reparou."
        ),
        "sections": [
            "Começamos pelo essencial: {topic} não é uma ideia abstrata. É uma decisão diária, tomada por pessoas comuns.",
            "O ponto de entrada em {topic} parece simples, mas cada escolha tem um custo escondido.",
            "É aqui que a escala muda. O que era raro há poucos anos passou a fazer parte do dia de milhões de pessoas.",
            "A resposta está nos detalhes: o que existe por trás de {topic} diz mais do que qualquer título de notícia.",
            "Nem tudo é sucesso. Há também o que falha, e é aí que {topic} se revela de forma mais clara.",
            "As primeiras pessoas a perceber {topic} agiram muito antes de existir prova de que iam resultar.",
            "O que antes exigia planeamento passou a resolver-se em segundos. A diferença é a escala, não a vontade.",
            "Há um custo escondido: cada ganho em {topic} traz um efeito secundário que quase ninguém mede.",
            "A transformação só fica completa quando {topic} deixa de ser novidade e passa a fazer parte da rotina.",
            "Por isso {topic} não é uma moda. É um sinal de que o comportamento das pessoas mudou de direção.",
        ],
    },
    "pt-BR": {
        "title": "{topic}: a história que quase ninguém viu",
        "hook": (
            "Em menos de um minuto, você vai entender por que {topic} mudou tudo, "
            "e quase ninguém percebeu."
        ),
        "sections": [
            "Começamos pelo essencial: {topic} não é uma ideia abstrata. É uma decisão diária, tomada por pessoas comuns.",
            "O ponto de entrada em {topic} parece simples, mas cada escolha tem um custo escondido.",
            "É aqui que a escala muda. O que era raro há poucos anos virou rotina para milhões de pessoas.",
            "A resposta está nos detalhes: o que existe por trás de {topic} diz mais do que qualquer manchete.",
            "Nem tudo é sucesso. Há também o que falha, e é aí que {topic} fica mais claro.",
            "As primeiras pessoas a entender {topic} agiram muito antes de existir prova de que iam funcionar.",
            "O que antes exigia planejamento agora é resolvido em segundos. A diferença é a escala, não a vontade.",
            "Existe um custo escondido: cada ganho em {topic} traz um efeito colateral que quase ninguém mede.",
            "A virada só se completa quando {topic} deixa de ser novidade e vira parte da rotina.",
            "Por isso {topic} não é modismo. É um sinal de que o comportamento das pessoas mudou de direção.",
        ],
    },
    "en-US": {
        "title": "{topic}: the story almost nobody saw",
        "hook": (
            "In under a minute you will see why {topic} changed everything, "
            "and almost nobody noticed."
        ),
        "sections": [
            "Start with the essentials: {topic} is not an abstract idea. It is a daily choice made by ordinary people.",
            "The entry point into {topic} looks simple, but every decision carries a hidden cost.",
            "This is where the scale changes. What was rare a few years ago is now routine for millions of people.",
            "The answer hides in the details: what sits behind {topic} says more than any headline.",
            "It is not all success. There is failure too, and that is where {topic} becomes clearest.",
            "The first people to understand {topic} acted long before there was proof it would work.",
            "What once required planning is now solved in seconds. The difference is scale, not intent.",
            "There is a hidden cost: every gain in {topic} brings a side effect almost nobody measures.",
            "The shift is only complete when {topic} stops being new and becomes part of the day.",
            "That is why {topic} is not a trend. It is a sign that behaviour has changed direction.",
        ],
    },
    "es-ES": {
        "title": "{topic}: la historia que casi nadie vio",
        "hook": (
            "En menos de un minuto entenderás por qué {topic} lo cambió todo, "
            "y casi nadie se dio cuenta."
        ),
        "sections": [
            "Empezamos por lo esencial: {topic} no es una idea abstracta. Es una decisión diaria de personas comunes.",
            "La puerta de entrada a {topic} parece simple, pero cada decisión tiene un coste oculto.",
            "Aquí es donde cambia la escala. Lo que era raro hace pocos años ya es rutina para millones de personas.",
            "La respuesta está en los detalles: lo que hay detrás de {topic} dice más que cualquier titular.",
            "No todo es éxito. También hay fracaso, y es justo ahí donde {topic} se revela con más claridad.",
            "Las primeras personas en entender {topic} actuaron mucho antes de que existiera prueba de que funcionaría.",
            "Lo que antes exigía planificación hoy se resuelve en segundos. La diferencia es la escala, no la voluntad.",
            "Hay un coste oculto: cada ganancia en {topic} trae un efecto secundario que casi nadie mide.",
            "La transformación solo se completa cuando {topic} deja de ser novedad y se vuelve rutina.",
            "Por eso {topic} no es una moda. Es una señal de que el comportamiento de la gente cambió de dirección.",
        ],
    },
    "fr-FR": {
        "title": "{topic} : l'histoire que presque personne n'a vue",
        "hook": (
            "En moins d'une minute, vous comprendrez pourquoi {topic} a tout changé, "
            "et presque personne ne l'a remarqué."
        ),
        "sections": [
            "Commençons par l'essentiel : {topic} n'est pas une idée abstraite. C'est un choix quotidien, pris par des gens ordinaires.",
            "Le point d'entrée dans {topic} paraît simple, mais chaque choix a un coût caché.",
            "C'est ici que l'échelle change. Ce qui était rare il y a peu est devenu la norme pour des millions de personnes.",
            "La réponse est dans les détails : ce qui se cache derrière {topic} en dit plus long qu'un titre d'actualité.",
            "Tout n'est pas réussite. Il y a aussi les échecs, et c'est là que {topic} se révèle le plus clairement.",
            "Les premières personnes à comprendre {topic} ont agi bien avant qu'une preuve de leur réussite existe.",
            "Ce qui demandait autrefois de la planification se règle désormais en quelques secondes. La différence est l'échelle.",
            "Il y a un coût caché : chaque gain en {topic} entraîne un effet de bord que presque personne ne mesure.",
            "La transformation n'est complète que lorsque {topic} cesse d'être une nouveauté et devient un réflexe.",
            "C'est pourquoi {topic} n'est pas une mode. C'est le signe que les comportements ont changé de direction.",
        ],
    },
}

# Concrete stock-search subjects per section position, in English. They are the
# floor under the fallback: a section whose narration yields no usable keyword
# still gets something a stock provider can actually return footage for.
_FALLBACK_VISUALS: List[List[str]] = [
    ["documentary camera crew", "city street at night"],
    ["close up hands working", "detailed texture"],
    ["crowd of people walking", "aerial city view"],
    ["macro detail object", "person thinking"],
    ["old photographs", "abandoned building"],
    ["team meeting whiteboard", "notebook and pen"],
    ["smartphone screen close up", "motion blur"],
    ["laboratory equipment", "stack of documents"],
    ["everyday home routine", "morning light window"],
    ["futuristic technology", "sunset city skyline"],
]

_TERM_WORD = re.compile(r"[\wÀ-ÿ]{3,}", re.UNICODE)


def _resolve_language(language: Any) -> str:
    code = str(language or "").strip()
    return code if code in _FALLBACK_TEMPLATES else DEFAULT_LANGUAGE


def _topic_terms(topic: str, limit: int = MAX_TERMS_PER_SECTION) -> List[str]:
    """Raw words of the topic, used when narration yields nothing searchable."""
    seen: List[str] = []
    for word in _TERM_WORD.findall(topic.lower()):
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def _derive_visual_terms(text: str, topic: str) -> List[str]:
    """Search terms for a fallback section, guaranteed non-empty.

    ``extract_keywords_from_text`` is the same extractor the stock search uses,
    so the fallback hands the downstream providers words they already handle.
    It drops words under four characters and Portuguese stopwords, which means a
    short topic (or a sentence built mostly of stopwords) can come back empty;
    the topic words and the per-position visual pool cover those cases.
    """
    terms = [
        term
        for term in pipeline.extract_keywords_from_text(text, limit=MAX_TERMS_PER_SECTION)
        if term and term not in STOPWORDS
    ]
    for candidate in _topic_terms(topic) + _FALLBACK_VISUALS[-1]:
        if len(terms) >= MAX_TERMS_PER_SECTION:
            break
        if candidate not in terms:
            terms.append(candidate)
    return terms


def build_local_script(
    topic: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    section_count: int = 5,
    tone: str = "documentary",
    duration_target: int = 60,
    fallback_error: Optional[str] = None,
) -> Script:
    """A usable script built with no provider involved at all.

    The narration is real prose assembled from per-language templates and the
    search terms are extracted from that prose, so the result feeds TTS, stock
    search and the edit plan exactly like a model answer would.
    """
    language = _resolve_language(language)
    templates = _FALLBACK_TEMPLATES[language]
    bodies: List[str] = templates["sections"]
    count = _clamp(section_count, MIN_SECTIONS, MAX_SECTIONS)

    sections: List[ScriptSection] = []
    for position in range(count):
        # Rotating through the pool keeps a short run from repeating the same
        # paragraph twice, which is what a plain slice would produce.
        body = bodies[position % len(bodies)].format(topic=topic)
        visuals = _derive_visual_terms(body, topic)
        for extra in _FALLBACK_VISUALS[position % len(_FALLBACK_VISUALS)]:
            if len(visuals) >= 2:
                break
            if extra not in visuals:
                visuals.append(extra)
        sections.append(
            ScriptSection(
                index=position + 1,
                text=body,
                visual_terms=visuals[:MAX_TERMS_PER_SECTION],
            )
        )

    return Script(
        title=templates["title"].format(topic=topic),
        hook=templates["hook"].format(topic=topic),
        sections=sections,
        language=language,
        tone=tone,
        source=SOURCE_FALLBACK,
        model=None,
        fallback_error=fallback_error,
    )


def generate_script(
    topic: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    section_count: int = 5,
    tone: str = "documentary",
    duration_target: int = 60,
    custom_instructions: str = "",
) -> Script:
    """Generate a full narration script for ``topic``. Synchronous.

    Delegates to ``pipeline.call_free_model``, which is itself synchronous (it
    drives its own event loop), so this is safe to call from a FastAPI sync
    endpoint. Provider problems never propagate: any failure produces a local
    template script with ``source="fallback-local"`` and ``fallback_error``
    explaining exactly what went wrong.

    Raises:
        ValueError: blank topic, or ``section_count`` outside 1..MAX_SECTIONS.
    """
    clean_topic = _clean_user_text(topic, MAX_TOPIC_CHARS)
    if not clean_topic:
        raise ValueError("Indique um tema para gerar o guião.")

    try:
        requested_sections = int(section_count)
    except (TypeError, ValueError):
        raise ValueError("section_count tem de ser um número inteiro.") from None
    if requested_sections < MIN_SECTIONS or requested_sections > MAX_SECTIONS:
        raise ValueError(f"section_count tem de estar entre {MIN_SECTIONS} e {MAX_SECTIONS}.")

    # Defensive clamps. The values above are already valid at this point, but the
    # prompt arithmetic and the fallback must never see a zero divisor or a
    # negative word budget if a caller bypasses the checks.
    sections_wanted = _clamp(requested_sections, MIN_SECTIONS, MAX_SECTIONS)
    try:
        target = int(duration_target)
    except (TypeError, ValueError):
        target = 60
    target = _clamp(target, MIN_DURATION, MAX_DURATION)

    resolved_language = _resolve_language(language)
    resolved_tone = _clean_user_text(tone, 60) or "documentary"
    instructions = _clean_user_text(custom_instructions, MAX_INSTRUCTIONS_CHARS)

    def local(reason: str) -> Script:
        return build_local_script(
            clean_topic,
            language=resolved_language,
            section_count=sections_wanted,
            tone=resolved_tone,
            duration_target=target,
            fallback_error=reason,
        )

    unavailable = _unavailable_reason()
    if unavailable:
        return local(unavailable)

    prompt = build_prompt(
        clean_topic,
        language=resolved_language,
        section_count=sections_wanted,
        tone=resolved_tone,
        duration_target=target,
        custom_instructions=instructions,
    )

    try:
        raw = pipeline.call_free_model(prompt, json_mode=True, max_tokens=1400)
    except (RuntimeError, ValueError) as exc:
        # AuthError subclasses RuntimeError, so a missing key, a rejected key and
        # an exhausted daily quota all land here with their message intact.
        return local(str(exc) or type(exc).__name__)

    template = _FALLBACK_TEMPLATES[resolved_language]
    try:
        parsed = _parse_model_answer(
            raw if isinstance(raw, str) else str(raw),
            section_count=sections_wanted,
            fallback_title=template["title"].format(topic=clean_topic),
            fallback_hook=template["hook"].format(topic=clean_topic),
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        return local(str(exc) or type(exc).__name__)

    return Script(
        title=parsed["title"],
        hook=parsed["hook"],
        sections=parsed["sections"],
        language=resolved_language,
        tone=resolved_tone,
        source=SOURCE_MODEL,
        model=_current_model(),
        fallback_error=None,
    )
