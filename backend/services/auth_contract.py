from __future__ import annotations

from typing import Any, Dict, Optional


AUTH_MISSING_KEY = "AUTH_MISSING_KEY"
AUTH_INVALID_KEY = "AUTH_INVALID_KEY"
AUTH_RATE_LIMIT = "AUTH_RATE_LIMIT"
AUTH_MODEL_NOT_FREE = "AUTH_MODEL_NOT_FREE"
AUTH_REQUEST_FAILED = "AUTH_REQUEST_FAILED"
AUTH_QUOTA_EXCEEDED = "AUTH_QUOTA_EXCEEDED"


class AuthError(RuntimeError):
    """Provider failure carrying a machine-readable code and HTTP status.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` fallbacks keep
    working while the structured fields are available to the API layer.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        status_code: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def missing_key_error(provider: str) -> AuthError:
    return AuthError(
        error_code=AUTH_MISSING_KEY,
        message=f"No API key configured for provider '{provider}'.",
        status_code=401,
        details={"provider": provider},
    )


def invalid_key_error(provider: str, details: Optional[Dict[str, Any]] = None) -> AuthError:
    return AuthError(
        error_code=AUTH_INVALID_KEY,
        message=f"API key rejected by provider '{provider}'.",
        status_code=403,
        details={"provider": provider, **(details or {})},
    )


def rate_limit_error(provider: str) -> AuthError:
    return AuthError(
        error_code=AUTH_RATE_LIMIT,
        message=f"Rate limit exceeded for provider '{provider}'.",
        status_code=429,
        details={"provider": provider},
    )


def model_not_free_error(model: str) -> AuthError:
    return AuthError(
        error_code=AUTH_MODEL_NOT_FREE,
        message=f"Model '{model}' is not free. Model name must end with ':free'.",
        status_code=400,
        details={"model": model},
    )


def request_failed_error(provider: str, exception: Exception) -> AuthError:
    return AuthError(
        error_code=AUTH_REQUEST_FAILED,
        message=f"Request to provider '{provider}' failed: {str(exception)}",
        status_code=502,
        details={"provider": provider, "exception": str(exception)},
    )


def handle_auth_error(error: AuthError) -> Dict[str, Any]:
    return {
        "status_code": error.status_code,
        "body": {
            "error_code": error.error_code,
            "message": error.message,
            "details": error.details,
        },
    }


def provider_status_error(
    provider: str,
    status: int,
    response_text: str = "",
    model: Optional[str] = None,
) -> AuthError:
    """Map a provider HTTP status onto the auth contract.

    Keeps the provider's own status code so the client can distinguish "your key
    is wrong" from "you are being throttled" without parsing the message.
    """
    details: Dict[str, Any] = {"provider": provider, "upstream_status": status}
    if model:
        details["model"] = model
    if response_text:
        details["upstream_body"] = response_text[:200]

    if status in (401, 403):
        return AuthError(
            AUTH_INVALID_KEY,
            f"Chave de {provider} rejeitada pelo fornecedor.",
            status,
            details,
        )
    if status == 402:
        return AuthError(
            AUTH_QUOTA_EXCEEDED,
            f"Créditos insuficientes na conta {provider}.",
            status,
            details,
        )
    if status == 429:
        return AuthError(
            AUTH_RATE_LIMIT,
            f"Rate limit atingido em {provider}. Tente novamente em instantes.",
            status,
            details,
        )
    return AuthError(
        AUTH_REQUEST_FAILED,
        f"{provider} respondeu HTTP {status}.",
        502 if status >= 500 else status,
        details,
    )


def to_response(error: AuthError) -> Dict[str, Any]:
    """JSON body shape consumed by the frontend auth handler."""
    return {
        "error_code": error.error_code,
        "message": error.message,
        "details": error.details,
    }
