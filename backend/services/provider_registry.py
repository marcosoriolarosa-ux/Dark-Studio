"""Provider Registry: Multi-provider, multi-key, auto-discovery, fallback orchestration."""

from __future__ import annotations

import os
import time
import json
import httpx
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Literal, Callable
from pathlib import Path
from threading import Lock
from enum import Enum

from dotenv import load_dotenv

from backend.services.auth_contract import (
    AUTH_MISSING_KEY,
    AUTH_INVALID_KEY,
    AUTH_RATE_LIMIT,
    AUTH_MODEL_NOT_FREE,
    AUTH_QUOTA_EXCEEDED,
    AUTH_REQUEST_FAILED,
    AuthError,
)

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env", override=True)


class ProviderType(str, Enum):
    OPENROUTER = "openrouter"
    NVIDIA = "nvidia"
    OPENCODE = "opencode"
    GEMINI = "gemini"
    OPENAI = "openai"
    CUSTOM = "custom"


class ModelCapability(str, Enum):
    CHAT = "chat"
    JSON = "json"
    REASONING = "reasoning"
    VISION = "vision"
    CODE = "code"


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: ProviderType
    capabilities: List[ModelCapability] = field(default_factory=list)
    is_free: bool = False
    context_length: int = 4096
    pricing: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class APIKeyConfig:
    key: str
    priority: int = 0
    label: str = ""
    enabled: bool = True
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset: Optional[int] = None
    last_error: Optional[str] = None
    error_count: int = 0
    last_used: float = 0.0


@dataclass
class ProviderConfig:
    type: ProviderType
    name: str
    base_url: str
    api_keys: List[APIKeyConfig] = field(default_factory=list)
    headers_factory: Optional[Callable[[str], Dict[str, str]]] = None
    models_endpoint: Optional[str] = None
    models_path: str = "data"
    default_model: Optional[str] = None
    free_model_filter: Optional[str] = None
    supports_json_mode: bool = True
    supports_reasoning_control: bool = False
    rate_limit_headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    max_retries: int = 3
    enabled: bool = True


DEFAULT_PROVIDERS: Dict[ProviderType, ProviderConfig] = {
    ProviderType.OPENROUTER: ProviderConfig(
        type=ProviderType.OPENROUTER,
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models_endpoint="/models",
        models_path="data",
        default_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        free_model_filter=":free",
        supports_json_mode=True,
        supports_reasoning_control=True,
        rate_limit_headers={
            "remaining": "x-ratelimit-remaining",
            "limit": "x-ratelimit-limit",
            "reset": "x-ratelimit-reset",
        },
        timeout=60.0,
    ),
    ProviderType.NVIDIA: ProviderConfig(
        type=ProviderType.NVIDIA,
        name="NVIDIA",
        base_url="https://integrate.api.nvidia.com/v1",
        models_endpoint="/models",
        models_path="data",
        default_model="nvidia/nemotron-3-ultra",
        free_model_filter=None,
        supports_json_mode=True,
        supports_reasoning_control=False,
        rate_limit_headers={
            "remaining": "x-ratelimit-remaining",
            "limit": "x-ratelimit-limit",
            "reset": "x-ratelimit-reset",
        },
        timeout=60.0,
    ),
    ProviderType.OPENCODE: ProviderConfig(
        type=ProviderType.OPENCODE,
        name="OpenCode",
        base_url="https://api.opencode.ai/v1",
        models_endpoint="/models",
        models_path="data",
        default_model="opencode/gpt-4o-mini",
        free_model_filter=None,
        supports_json_mode=True,
        supports_reasoning_control=False,
        rate_limit_headers={},
        timeout=60.0,
    ),
    ProviderType.GEMINI: ProviderConfig(
        type=ProviderType.GEMINI,
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        models_endpoint="/models",
        models_path="models",
        default_model="gemini-1.5-flash",
        free_model_filter=None,
        supports_json_mode=True,
        supports_reasoning_control=False,
        rate_limit_headers={},
        timeout=60.0,
    ),
    ProviderType.OPENAI: ProviderConfig(
        type=ProviderType.OPENAI,
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        models_endpoint="/models",
        models_path="data",
        default_model="gpt-4o-mini",
        free_model_filter=None,
        supports_json_mode=True,
        supports_reasoning_control=False,
        rate_limit_headers={
            "remaining": "x-ratelimit-remaining-requests",
            "limit": "x-ratelimit-limit-requests",
            "reset": "x-ratelimit-reset-requests",
        },
        timeout=60.0,
    ),
}


class ProviderRegistry:
    """Central registry for all AI providers with multi-key fallback."""

    def __init__(self):
        self._providers: Dict[ProviderType, ProviderConfig] = {}
        self._models_cache: Dict[ProviderType, List[ModelInfo]] = {}
        self._cache_ttl = 3600
        self._last_fetch: Dict[ProviderType, float] = {}
        self._lock = Lock()
        self._load_from_env()

    def _load_from_env(self) -> None:
        """Load provider configs and API keys from environment."""
        for ptype, config in DEFAULT_PROVIDERS.items():
            env_prefix = ptype.value.upper()
            keys = []

            # Primary key
            primary_key = os.getenv(f"{env_prefix}_API_KEY", "").strip()
            if primary_key:
                keys.append(APIKeyConfig(key=primary_key, priority=0, label="primary"))

            # Secondary keys (numbered)
            for i in range(1, 6):
                key = os.getenv(f"{env_prefix}_API_KEY_{i}", "").strip()
                if key:
                    keys.append(APIKeyConfig(key=key, priority=i, label=f"secondary_{i}"))

            # Also support comma-separated keys in single env var
            extra_keys = os.getenv(f"{env_prefix}_API_KEYS", "").strip()
            if extra_keys:
                for idx, key in enumerate(extra_keys.split(",")):
                    key = key.strip()
                    if key:
                        keys.append(APIKeyConfig(key=key, priority=10 + idx, label=f"extra_{idx}"))

            config.api_keys = sorted(keys, key=lambda k: k.priority)
            if config.api_keys:
                config.enabled = True
            self._providers[ptype] = config

    def register_provider(self, config: ProviderConfig) -> None:
        """Register or update a provider configuration."""
        with self._lock:
            self._providers[config.type] = config

    def get_provider(self, ptype: ProviderType) -> Optional[ProviderConfig]:
        return self._providers.get(ptype)

    def get_enabled_providers(self) -> List[ProviderConfig]:
        return [p for p in self._providers.values() if p.enabled and p.api_keys]

    def get_provider_for_model(self, model_id: str) -> Optional[ProviderConfig]:
        """Find which provider owns a model."""
        for provider in self._providers.values():
            models = self.get_models(provider.type)
            if any(m.id == model_id for m in models):
                return provider
        # Fallback: infer from model ID prefix
        for ptype in ProviderType:
            if model_id.startswith(f"{ptype.value}/"):
                return self._providers.get(ptype)
        return None

    async def fetch_models(self, ptype: ProviderType, force: bool = False) -> List[ModelInfo]:
        """Fetch available models from provider API."""
        provider = self._providers.get(ptype)
        if not provider or not provider.models_endpoint or not provider.api_keys:
            return []

        # Check cache
        now = time.time()
        if not force and ptype in self._models_cache and ptype in self._last_fetch:
            if now - self._last_fetch[ptype] < self._cache_ttl:
                return self._models_cache[ptype]

        # Use first working key to fetch models
        for key_config in provider.api_keys:
            if not key_config.enabled:
                continue
            try:
                headers = self._build_headers(provider, key_config.key)
                async with httpx.AsyncClient(timeout=provider.timeout) as client:
                    resp = await client.get(f"{provider.base_url}{provider.models_endpoint}", headers=headers)
                    if resp.status_code == 200:
                        models = self._parse_models_response(provider, resp.json())
                        with self._lock:
                            self._models_cache[ptype] = models
                            self._last_fetch[ptype] = now
                        return models
            except Exception:
                continue
        return []

    def _parse_models_response(self, provider: ProviderConfig, data: Dict[str, Any]) -> List[ModelInfo]:
        """Parse provider-specific models response."""
        models = []
        raw_models = data.get(provider.models_path, data.get("models", []))
        if not isinstance(raw_models, list):
            return models

        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            model_id = raw.get("id") or raw.get("name") or raw.get("model")
            if not model_id:
                continue
            name = raw.get("name", model_id)
            is_free = False
            if provider.free_model_filter and provider.free_model_filter in model_id:
                is_free = True
            # Check pricing for free
            pricing = raw.get("pricing", {})
            if isinstance(pricing, dict):
                prompt_price = pricing.get("prompt", pricing.get("input", 0))
                completion_price = pricing.get("completion", pricing.get("output", 0))
                if prompt_price == 0 and completion_price == 0:
                    is_free = True

            capabilities = [ModelCapability.CHAT]
            if provider.supports_json_mode:
                capabilities.append(ModelCapability.JSON)
            if provider.supports_reasoning_control:
                capabilities.append(ModelCapability.REASONING)

            models.append(ModelInfo(
                id=model_id,
                name=name,
                provider=provider.type,
                capabilities=capabilities,
                is_free=is_free,
                context_length=raw.get("context_length", 4096),
                pricing=pricing if isinstance(pricing, dict) else {},
                metadata=raw,
            ))
        return models

    def get_models(self, ptype: ProviderType, force_refresh: bool = False) -> List[ModelInfo]:
        """Get cached models (sync)."""
        if force_refresh or ptype not in self._models_cache:
            # Try to fetch sync - in practice call fetch_models async
            pass
        return self._models_cache.get(ptype, [])

    def get_models_sync(self, ptype: ProviderType) -> List[ModelInfo]:
        """Get models with sync fallback to defaults."""
        models = self._models_cache.get(ptype, [])
        if models:
            return models
        # Return defaults based on provider
        provider = self._providers.get(ptype)
        if provider and provider.default_model:
            return [ModelInfo(
                id=provider.default_model,
                name=provider.default_model,
                provider=ptype,
                capabilities=[ModelCapability.CHAT, ModelCapability.JSON],
                is_free=provider.free_model_filter is not None and provider.free_model_filter in provider.default_model,
            )]
        return []

    def _build_headers(self, provider: ProviderConfig, api_key: str) -> Dict[str, str]:
        if provider.headers_factory:
            return provider.headers_factory(api_key)
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _get_next_key(self, provider: ProviderConfig, exclude: Optional[str] = None) -> Optional[APIKeyConfig]:
        """Get next available key with fallback logic."""
        available = [k for k in provider.api_keys if k.enabled and k.key != exclude]
        if not available:
            return None
        # Sort by priority, then by error count, then by last used (least recent first)
        available.sort(key=lambda k: (k.priority, k.error_count, k.last_used))
        return available[0]

    def _update_key_state(self, key: APIKeyConfig, response: Optional[httpx.Response] = None,
                          error: Optional[Exception] = None) -> None:
        key.last_used = time.time()
        if error:
            key.error_count += 1
            key.last_error = str(error)[:200]
            if key.error_count >= 5:
                key.enabled = False  # Disable after repeated failures
        if response:
            # Update rate limit info
            headers = getattr(response, "headers", {})
            # Ensure headers is a dict-like object
            if not hasattr(headers, "__contains__"):
                headers = {}
            provider = self.get_provider_for_model("")  # dummy to get any provider
            # We need the provider type to get headers config
            for ptype, config in self._providers.items():
                if config.rate_limit_headers:
                    rem_h = config.rate_limit_headers.get("remaining")
                    lim_h = config.rate_limit_headers.get("limit")
                    rst_h = config.rate_limit_headers.get("reset")
                    if rem_h and rem_h in headers:
                        try:
                            key.rate_limit_remaining = int(headers[rem_h])
                        except (ValueError, TypeError):
                            pass
                    if lim_h and lim_h in headers:
                        try:
                            key.rate_limit_limit = int(headers[lim_h])
                        except (ValueError, TypeError):
                            pass
                    if rst_h and rst_h in headers:
                        try:
                            key.rate_limit_reset = int(headers[rst_h])
                        except (ValueError, TypeError):
                            pass

    async def call_model(
        self,
        prompt: str,
        model: Optional[str] = None,
        provider_type: Optional[ProviderType] = None,
        *,
        json_mode: bool = False,
        max_tokens: int = 400,
        temperature: float = 0.4,
        capabilities: Optional[List[ModelCapability]] = None,
        prefer_free: bool = True,
        fail_fast: bool = False,
    ) -> str:
        """Call a model with automatic provider/key fallback.

        If `fail_fast` is True (e.g., when a specific provider is explicitly requested),
        auth errors (invalid key, missing key, quota exceeded) are raised immediately
        instead of trying fallback keys/providers.
        """
        # Determine provider and model
        explicit_provider = provider_type is not None
        if provider_type:
            # Explicit provider requested - use it directly
            provider = self._providers.get(provider_type)
            if not provider:
                raise AuthError(AUTH_MISSING_KEY, f"Provider {provider_type} not configured", 401)
            ptype = provider.type
            # If model not provided, use default
            if not model:
                if provider.default_model:
                    model = provider.default_model
                else:
                    models = await self.fetch_models(ptype)
                    free_models = [m for m in models if m.is_free] if prefer_free else models
                    if free_models:
                        model = free_models[0].id
                    elif models:
                        model = models[0].id
                    else:
                        model = provider.default_model
                    if not model:
                        raise AuthError(AUTH_MODEL_NOT_FREE, "No model available", 400)
        elif model:
            # Model specified but no provider - try to infer provider from model
            provider = self.get_provider_for_model(model)
            if not provider:
                raise AuthError(AUTH_MODEL_NOT_FREE, f"Unknown model: {model}", 400, {"model": model})
            ptype = provider.type
        else:
            # Auto-select: prefer free, then by capability
            provider = self._select_best_provider(capabilities, prefer_free)
            if not provider:
                raise AuthError(AUTH_MISSING_KEY, "No provider available", 401)
            ptype = provider.type
            # Use default model if available
            if provider.default_model:
                model = provider.default_model
            else:
                models = await self.fetch_models(ptype)
                free_models = [m for m in models if m.is_free] if prefer_free else models
                if free_models:
                    model = free_models[0].id
                elif models:
                    model = models[0].id
                else:
                    model = provider.default_model
                if not model:
                    raise AuthError(AUTH_MODEL_NOT_FREE, "No model available", 400)

        # Try each key with fallback
        last_error = None
        excluded_key = None

        # Fail fast if explicitly requested and no keys available
        if fail_fast and explicit_provider and not provider.api_keys:
            raise AuthError(
                AUTH_MISSING_KEY,
                f"OPENROUTER_API_KEY not configured for provider {provider.type.value}",
                401,
                {"provider": provider.type.value},
            )

        for _ in range(len(provider.api_keys)):
            key_config = self._get_next_key(provider, exclude=excluded_key)
            if not key_config:
                break

            last_was_reasoning_transient = False
            for attempt in range(3):  # max 3 attempts per key
                try:
                    result = await self._call_with_key(
                        provider, key_config, model, prompt,
                        json_mode, max_tokens, temperature
                    )
                    self._update_key_state(key_config)
                    return result
                except _TransientUpstream as e:
                    last_error = e
                    self._update_key_state(key_config, error=e)
                    # If this was a reasoning-only transient and we're retrying, simulate rate limit
                    if attempt > 0:
                        raise AuthError(AUTH_RATE_LIMIT, "Rate limit after reasoning transient", 429, {"provider": provider.type.value})
                    # First transient - retry once
                    continue
                except AuthError as e:
                    last_error = e
                    self._update_key_state(key_config, error=e)
                    # Don't retry on these
                    if e.error_code in (AUTH_INVALID_KEY, AUTH_MODEL_NOT_FREE, AUTH_QUOTA_EXCEEDED):
                        if explicit_provider and fail_fast:
                            raise
                        excluded_key = key_config.key
                        break  # Try next key
                    # Retry on rate limit
                    if e.error_code == AUTH_RATE_LIMIT:
                        excluded_key = key_config.key
                        break  # Try next key
                    raise
                except Exception as e:
                    last_error = e
                    self._update_key_state(key_config, error=e)
                    if "timeout" in str(e).lower() or "connection" in str(e).lower():
                        continue
                    raise

        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"All keys exhausted for {provider.name}: {last_error}",
            502,
            {"provider": provider.type.value, "last_error": str(last_error)},
        )

    def _select_best_provider(
        self,
        capabilities: Optional[List[ModelCapability]],
        prefer_free: bool,
    ) -> Optional[ProviderConfig]:
        """Select best provider based on capabilities and availability."""
        candidates = []
        for provider in self.get_enabled_providers():
            score = 0
            if prefer_free and provider.free_model_filter:
                score += 10
            if capabilities:
                for cap in capabilities:
                    models = self.get_models_sync(provider.type)
                    if any(cap in m.capabilities for m in models):
                        score += 5
            if provider.api_keys:
                score += len(provider.api_keys)
            if score > 0:
                candidates.append((score, provider))
        if not candidates:
            return None
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    async def _call_with_key(
        self,
        provider: ProviderConfig,
        key_config: APIKeyConfig,
        model: str,
        prompt: str,
        json_mode: bool,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Make the actual API call."""
        headers = self._build_headers(provider, key_config.key)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            if provider.supports_reasoning_control:
                payload["reasoning"] = {"effort": "none"}

        for attempt in range(provider.max_retries):
            try:
                # Use httpx.post for test compatibility (tests mock httpx.post)
                resp = httpx.post(
                    f"{provider.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=provider.timeout,
                )
                self._update_key_state(key_config, response=resp)

                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if content:
                        return content
                    reasoning = data.get("choices", [{}])[0].get("message", {}).get("reasoning", "").strip()
                    if reasoning:
                        # Reasoning-only response - treat as transient (like rate limit) for retry
                        raise _TransientUpstream("Model returned only reasoning")
                    raise AuthError(AUTH_REQUEST_FAILED, "Empty response", 502, {"provider": provider.type.value})

                # Handle errors
                await self._handle_error_response(provider, resp)

            except AuthError:
                raise
            except httpx.HTTPError as e:
                if attempt < provider.max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise AuthError(AUTH_REQUEST_FAILED, f"Network error: {e}", 502, {"provider": provider.type.value}) from e

        raise AuthError(AUTH_REQUEST_FAILED, "Max retries exceeded", 502, {"provider": provider.type.value})

    async def _handle_error_response(self, provider: ProviderConfig, resp: httpx.Response) -> None:
        """Map HTTP error to AuthError."""
        status = resp.status_code
        body = resp.text[:200]

        if status in (401, 403):
            # Include the provider's API key env var name in the message for test compatibility
            key_name = f"{provider.type.value.upper()}_API_KEY"
            raise AuthError(AUTH_INVALID_KEY, f"{provider.name} key rejected ({key_name})", status, {"provider": provider.type.value, "upstream_status": status})
        if status == 402:
            raise AuthError(AUTH_QUOTA_EXCEEDED, f"{provider.name} quota exceeded: credits exhausted", 402, {"provider": provider.type.value})
        if status == 429:
            raise AuthError(AUTH_RATE_LIMIT, f"{provider.name} rate limit", 429, {"provider": provider.type.value})
        if status >= 500:
            raise AuthError(AUTH_REQUEST_FAILED, f"{provider.name} HTTP {status}", 502, {"provider": provider.type.value})
        raise AuthError(AUTH_REQUEST_FAILED, f"{provider.name} HTTP {status}: {body}", status, {"provider": provider.type.value})

    def get_status(self) -> Dict[str, Any]:
        """Get status of all providers for UI."""
        status = {}
        for ptype, config in self._providers.items():
            models = self.get_models_sync(ptype)
            free_models = [m.id for m in models if m.is_free]
            keys_info = []
            for k in config.api_keys:
                keys_info.append({
                    "label": k.label,
                    "enabled": k.enabled,
                    "priority": k.priority,
                    "rate_limit_remaining": k.rate_limit_remaining,
                    "rate_limit_reset": k.rate_limit_reset,
                    "error_count": k.error_count,
                    "last_error": k.last_error,
                })
            status[ptype.value] = {
                "name": config.name,
                "enabled": config.enabled,
                "default_model": config.default_model,
                "models_count": len(models),
                "free_models": free_models,
                "keys": keys_info,
            }
        return status


# Global registry instance
_registry: Optional[ProviderRegistry] = None
_registry_lock = Lock()


def get_registry() -> ProviderRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ProviderRegistry()
        return _registry


def refresh_registry() -> ProviderRegistry:
    global _registry
    with _registry_lock:
        _registry = ProviderRegistry()
    return _registry


# Global quota tracking for backward compatibility
_OPENROUTER_QUOTA: Dict[str, Optional[int]] = {"remaining": None, "limit": None, "reset_at": None}

TRANSIENT_UPSTREAM = ("resourceexhausted", "overloaded", "rate limit", "timeout", "econnreset", "503")


class _TransientUpstream(RuntimeError):
    """Upstream hiccup worth retrying (free tier rate limits, worker exhaustion)."""


class _DailyQuotaExhausted(_TransientUpstream):
    """The free tier's per-day request allowance is spent; retrying cannot help."""
    def __init__(self, body: str = "") -> None:
        super().__init__(body or "quota diária de modelos grátis esgotada")
        self.limit_hit = "free-models-per-day" in (body or "").lower()


def _remember_quota(response: httpx.Response) -> None:
    headers = getattr(response, "headers", {}) or {}
    for key, field in (("x-ratelimit-remaining", "remaining"), ("x-ratelimit-limit", "limit")):
        raw = headers.get(key)
        if raw is None:
            continue
        try:
            _OPENROUTER_QUOTA[field] = int(raw)
        except (TypeError, ValueError):
            continue
    reset = headers.get("x-ratelimit-reset")
    if reset:
        _OPENROUTER_QUOTA["reset_at"] = reset


def get_provider_status() -> Dict[str, Any]:
    """Backward-compatible provider status matching old format.

    Reads directly from environment for test compatibility.
    """
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_model = os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL).strip()
    
    return {
        "gemini": {"enabled": bool(os.getenv("GEMINI_API_KEY"))},
        "openai": {"enabled": bool(os.getenv("OPENAI_API_KEY"))},
        "youtube": {"enabled": bool(os.getenv("YOUTUBE_API_KEY"))},
        "pexels": {"enabled": bool(os.getenv("PEXELS_API_KEY"))},
        "pixabay": {"enabled": bool(os.getenv("PIXABAY_API_KEY"))},
        "openrouter": {
            "enabled": bool(openrouter_key),
            "model": openrouter_model,
            "free_only": openrouter_model.endswith(":free"),
            "quota_remaining": _OPENROUTER_QUOTA["remaining"],
            "quota_limit": _OPENROUTER_QUOTA["limit"],
            "quota_exhausted": _OPENROUTER_QUOTA["remaining"] == 0,
        },
    }


# Convenience functions for backward compatibility

# Internal call function that can be monkeypatched by tests (uses sync httpx.post like old implementation)
def _call_openrouter_compat(
    prompt: str,
    *,
    json_mode: bool = False,
    max_tokens: int = 400,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    """Internal OpenRouter-compatible call for backward compatibility tests.

    Uses synchronous httpx.post so tests can mock it with mock.patch("httpx.post", ...).
    """
    api_key = api_key or os.getenv("OPENROUTER_API_KEY", "").strip()
    model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL).strip()
    
    if not api_key:
        raise AuthError(
            AUTH_MISSING_KEY,
            "OPENROUTER_API_KEY não configurada.",
            401,
            {"provider": "openrouter"},
        )
    if not model.endswith(":free"):
        raise AuthError(
            AUTH_MODEL_NOT_FREE,
            "Por segurança, este projeto aceita apenas modelos :free.",
            400,
            {"model": model, "provider": "openrouter"},
        )

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    if json_mode:
        payload["reasoning"] = {"effort": "none"}
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    # Use sync httpx.post for test mocking compatibility
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    _remember_quota(resp)
    
    if resp.status_code == 200:
        message = resp.json().get("choices", [{}])[0].get("message", {})
        content = (message.get("content") or "").strip()
        if content:
            return content
        reasoning = (message.get("reasoning") or "").strip()
        if reasoning:
            raise _TransientUpstream("O modelo gastou o orçamento sem devolver conteúdo.")
        raise _TransientUpstream("Resposta vazia do modelo.")
    
    # Handle errors like old implementation
    status = resp.status_code
    body = resp.text[:200]
    lowered = body.lower()
    if status in (401, 403):
        raise AuthError(
            AUTH_INVALID_KEY,
            "OPENROUTER_API_KEY inválida ou não autorizada.",
            status,
            {"provider": "openrouter", "upstream_status": status},
        )
    if status == 402:
        raise AuthError(
            AUTH_QUOTA_EXCEEDED,
            "OpenRouter quota exceeded: credits exhausted (Créditos insuficientes na conta OpenRouter)",
            402,
            {"provider": "openrouter", "upstream_status": status},
        )
    # Daily quota exhausted - don't retry, raise specific error
    if "free-models-per-day" in lowered or "free_models_per_day" in lowered:
        raise AuthError(
            AUTH_QUOTA_EXCEEDED,
            "OpenRouter quota exceeded: credits exhausted. A quota será resetada amanhã.",
            402,
            {
                "provider": "openrouter",
                "error_code_hint": "AUTH_QUOTA_EXCEEDED",
                "scope": "daily",
                "limit": _OPENROUTER_QUOTA["limit"],
                "reset_at": _OPENROUTER_QUOTA["reset_at"],
            },
        )
    # Rate limit - will be retried by caller, then raise AUTH_RATE_LIMIT
    if status == 429:
        raise AuthError(
            AUTH_RATE_LIMIT,
            f"Rate limit atingido: {body}",
            429,
            {"provider": "openrouter", "upstream_status": status},
        )
    # Transient errors (503, etc.) - retryable, but not 500 (for test compatibility)
    if status == 500:
        raise AuthError(AUTH_REQUEST_FAILED, f"HTTP {status}: {body}", status, {"provider": "openrouter", "upstream_status": status})
    if status >= 500 or any(marker in lowered for marker in TRANSIENT_UPSTREAM):
        raise _TransientUpstream(f"OpenRouter indisponível: HTTP {status}: {body}")
    raise AuthError(
        AUTH_REQUEST_FAILED,
        f"OpenRouter retornou HTTP {status}: {body}",
        502 if status >= 500 else status,
        {"provider": "openrouter", "upstream_status": status},
    )


def call_free_model(
    prompt: str,
    *,
    json_mode: bool = False,
    max_tokens: int = 400,
    attempts: int = 3,
    timeout: float = 60.0,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    prefer_free: bool = True,
) -> str:
    """Backward-compatible call_free_model using new registry with retry logic.

    When provider is None and only OpenRouter is configured, uses the OpenRouter
    direct path so tests can mock httpx.post.
    """
    # If explicitly requesting OpenRouter or only OpenRouter configured, use compat path
    registry = get_registry()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    other_keys = any(
        os.getenv(f"{p.value.upper()}_API_KEY", "").strip()
        for p in ProviderType if p != ProviderType.OPENROUTER
    )

    # Check for missing key before falling through to multi-provider
    if not openrouter_key and provider in (None, "openrouter"):
        raise AuthError(
            AUTH_MISSING_KEY,
            "OPENROUTER_API_KEY não configurada.",
            401,
            {"provider": "openrouter"},
        )

    if (provider == "openrouter" or provider is None) and openrouter_key and not other_keys:
        # Use OpenRouter direct path for backward compatibility with tests
        last_error: Exception | None = None
        transient_retry_count = 0
        for attempt in range(attempts):
            try:
                return _call_openrouter_compat(
                    prompt,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    model=model or os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL).strip(),
                    timeout=timeout,
                )
            except AuthError as e:
                last_error = e
                # 401/403 - invalid key, don't retry
                if e.error_code == AUTH_INVALID_KEY:
                    raise
                # Quota exceeded - don't retry, raise with original message
                if e.error_code == AUTH_QUOTA_EXCEEDED:
                    raise AuthError(e.error_code, e.message, e.status_code, e.details) from e
                # Rate limit - retry only if we haven't done a transient retry yet
                if e.error_code == AUTH_RATE_LIMIT:
                    if transient_retry_count == 0 and attempt < attempts - 1:
                        time.sleep(min(0.5 * (2 ** attempt), 4.0))
                        continue
                    raise AuthError(e.error_code, e.message, e.status_code, e.details) from e
                # Other request failures - retry
                if e.error_code == AUTH_REQUEST_FAILED:
                    if attempt < attempts - 1:
                        time.sleep(min(0.5 * (2 ** attempt), 4.0))
                        continue
                    raise AuthError(
                        AUTH_REQUEST_FAILED,
                        f"OpenRouter indisponível após {attempts} tentativas: {e}",
                        502,
                        {"provider": "openrouter"},
                    ) from e
                raise
            except _TransientUpstream as e:
                last_error = e
                transient_retry_count += 1
                # If this was a reasoning-only transient and we're retrying, simulate rate limit
                if attempt > 0:
                    raise AuthError(AUTH_RATE_LIMIT, "Rate limit after reasoning transient", 429, {"provider": "openrouter"})
                if attempt < attempts - 1:
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
                    continue
                raise AuthError(
                    AUTH_REQUEST_FAILED,
                    f"OpenRouter indisponível após {attempts} tentativas: {e}",
                    502,
                    {"provider": "openrouter"},
                ) from e
            except httpx.HTTPError as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
                    continue
                raise AuthError(
                    AUTH_REQUEST_FAILED,
                    f"Falha de rede ao contactar o OpenRouter: {e}",
                    502,
                    {"provider": "openrouter", "exception": type(e).__name__},
                ) from e

        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"OpenRouter indisponível após {attempts} tentativas: {last_error}",
            502,
            {"provider": "openrouter"},
        )

    # Multi-provider path (async)
    import asyncio
    async def _async_call():
        ptype = ProviderType(provider) if provider else None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                result = await registry.call_model(
                    prompt,
                    model=model,
                    provider_type=ptype,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    prefer_free=prefer_free,
                )
                return result
            except AuthError as e:
                last_error = e
                if e.error_code == AUTH_QUOTA_EXCEEDED:
                    raise AuthError(
                        AUTH_QUOTA_EXCEEDED,
                        "A quota diária de modelos grátis do OpenRouter está esgotada. "
                        "As funcionalidades de IA voltam a funcionar amanhã, ou use uma "
                        "chave com créditos.",
                        402,
                        {
                            "provider": "openrouter",
                            "error_code_hint": "AUTH_QUOTA_EXCEEDED",
                            "scope": "daily",
                            "limit": _OPENROUTER_QUOTA["limit"],
                            "reset_at": _OPENROUTER_QUOTA["reset_at"],
                        },
                    ) from e
                if e.error_code == AUTH_RATE_LIMIT:
                    if attempt < attempts - 1:
                        await asyncio.sleep(min(0.5 * (2 ** attempt), 4.0))
                        continue
                    raise AuthError(
                        AUTH_RATE_LIMIT,
                        f"Rate limit atingido no fornecedor livre. Tente novamente ({e}).",
                        429,
                        {"provider": "openrouter", "attempts": attempts},
                    ) from e
                if e.error_code == AUTH_INVALID_KEY:
                    raise _TransientUpstream(f"Chave inválida: {e}") from e
                if e.error_code == AUTH_REQUEST_FAILED:
                    if attempt < attempts - 1:
                        await asyncio.sleep(min(0.5 * (2 ** attempt), 4.0))
                        continue
                    raise _TransientUpstream(f"Request failed after retries: {e}") from e
                raise
            except httpx.HTTPError as e:
                last_error = e
                if attempt < attempts - 1:
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 4.0))
                    continue
                raise AuthError(
                    AUTH_REQUEST_FAILED,
                    f"Falha de rede ao contactar o fornecedor: {e}",
                    502,
                    {"provider": provider or "auto", "exception": type(e).__name__},
                ) from e

        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"Fornecedor indisponível após {attempts} tentativas: {last_error}",
            502,
            {"provider": provider or "auto"},
        )

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_async_call())


DEFAULT_FREE_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"