/*
 * Dark Video Studio — Auth Error Handler
 * -------------------------------------
 * Self-contained auth error handling for the frontend.
 * Works with vanilla JS (window.AuthHandler), ES modules, and CommonJS.
 *
 * Error codes handled:
 *   AUTH_MISSING_KEY      — no API key configured
 *   AUTH_INVALID_KEY      — key rejected by provider
 *   AUTH_RATE_LIMIT       — too many requests (retryable)
 *   AUTH_QUOTA_EXCEEDED   — quota/billing limit reached
 *   AUTH_SERVER_ERROR     — provider 5xx
 *   AUTH_NETWORK_ERROR    — connection failed
 *   AUTH_TIMEOUT          — request timed out
 *   AUTH_UNKNOWN          — fallback for anything else
 *
 * UI feedback modes: 'toast' | 'banner' | 'modal' | 'silent'
 */

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.AuthHandler = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const ERROR_MESSAGES = {
    AUTH_MISSING_KEY: 'Chave de API em falta. Adicione-a nas configurações para continuar.',
    AUTH_INVALID_KEY: 'Chave de API rejeitada. Verifique a chave nas configurações.',
    AUTH_RATE_LIMIT: 'Limite de pedidos atingido. A tentar novamente dentro de momentos.',
    AUTH_QUOTA_EXCEEDED: 'Cota da API esgotada. Actualize o plano ou amanhã tente novamente.',
    AUTH_SERVER_ERROR: 'O fornecedor de IA está com problemas. Tente novamente mais tarde.',
    AUTH_NETWORK_ERROR: 'Sem ligação ao servidor. Verifique a sua rede e tente novamente.',
    AUTH_TIMEOUT: 'O pedido demorou demasiado. Verifique a ligação e tente novamente.',
    AUTH_UNKNOWN: 'Ocorreu um erro inesperado. Tente novamente ou contacte o suporte.',
  };

  const RETRYABLE_CODES = new Set(['AUTH_RATE_LIMIT', 'AUTH_SERVER_ERROR', 'AUTH_NETWORK_ERROR', 'AUTH_TIMEOUT']);

  const SETTINGS_REDIRECT_CODES = new Set(['AUTH_MISSING_KEY', 'AUTH_INVALID_KEY']);

  const DEFAULT_OPTIONS = {
    maxRetries: 3,
    baseDelayMs: 1000,
    maxDelayMs: 15000,
    timeoutMs: 30000,
    settingsPath: '/#settings',
    feedback: 'toast',
    onRedirect: null,
    onFeedback: null,
  };

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function extractErrorCode(body, status) {
    if (body && typeof body === 'object') {
      const code = body.code || body.error_code || body.error;
      if (typeof code === 'string' && code.toUpperCase().startsWith('AUTH_')) {
        return code.toUpperCase();
      }
      if (body.detail && typeof body.detail === 'string') {
        const match = body.detail.match(/AUTH_[A-Z_]+/);
        if (match) return match[0].toUpperCase();
      }
    }
    if (status === 401 || status === 403) return 'AUTH_INVALID_KEY';
    if (status === 429) return 'AUTH_RATE_LIMIT';
    if (status >= 500) return 'AUTH_SERVER_ERROR';
    return 'AUTH_UNKNOWN';
  }

  class AuthErrorHandler {
    constructor(options = {}) {
      this.options = { ...DEFAULT_OPTIONS, ...options };
    }

    parse(response, body) {
      const code = extractErrorCode(body, response.status);
      return {
        code,
        status: response.status,
        message: getErrorMessage(code),
        detail: body && body.detail ? body.detail : null,
        retryable: shouldRetry(code),
        requiresSettings: SETTINGS_REDIRECT_CODES.has(code),
        raw: body,
      };
    }

    async handle(response, body) {
      const error = this.parse(response, body);
      this.showFeedback(error);
      if (error.requiresSettings) {
        this.redirectToSettings(error.code);
      }
      return error;
    }

    showFeedback(error) {
      if (typeof this.options.onFeedback === 'function') {
        this.options.onFeedback(error);
        return;
      }
      const ui = createFeedbackUI(this.options.feedback);
      ui(error);
    }

    redirectToSettings(errorCode) {
      if (typeof this.options.onRedirect === 'function') {
        this.options.onRedirect(errorCode);
        return;
      }
      if (typeof window !== 'undefined' && window.location) {
        const separator = this.options.settingsPath.includes('?') ? '&' : '?';
        window.location.href = `${this.options.settingsPath}${separator}auth_error=${encodeURIComponent(errorCode)}`;
      }
    }
  }

  function getErrorMessage(errorCode) {
    return ERROR_MESSAGES[errorCode] || ERROR_MESSAGES.AUTH_UNKNOWN;
  }

  function shouldRetry(errorCode) {
    return RETRYABLE_CODES.has(errorCode);
  }

  function redirectToSettings(errorCode, settingsPath = DEFAULT_OPTIONS.settingsPath) {
    if (!SETTINGS_REDIRECT_CODES.has(errorCode)) return false;
    if (typeof window !== 'undefined' && window.location) {
      const separator = settingsPath.includes('?') ? '&' : '?';
      window.location.href = `${settingsPath}${separator}auth_error=${encodeURIComponent(errorCode)}`;
      return true;
    }
    return false;
  }

  async function handleApiError(response, options = {}) {
    const handler = new AuthErrorHandler(options);
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    return handler.handle(response, body);
  }

  function createFeedbackUI(mode) {
    if (mode === 'silent') return () => {};
    if (mode === 'banner') return showBanner;
    if (mode === 'modal') return showModal;
    return showToast;
  }

  function ensureContainer(id) {
    let el = document.getElementById(id);
    if (!el) {
      el = document.createElement('div');
      el.id = id;
      el.style.cssText =
        'position:fixed;z-index:9999;font:13px/1.4 "Segoe UI",sans-serif;pointer-events:none';
      document.body.appendChild(el);
    }
    return el;
  }

  function showToast(error) {
    const container = ensureContainer('auth-toast-container');
    container.style.cssText += ';top:16px;right:16px;display:flex;flex-direction:column;gap:8px;max-width:min(380px,calc(100vw - 32px))';
    const toast = document.createElement('div');
    const color = error.retryable ? '#fbbf24' : '#f87171';
    toast.style.cssText = `pointer-events:auto;background:#0e1219;border:1px solid ${color};border-left:4px solid ${color};border-radius:10px;padding:12px 14px;color:#e8eef7;box-shadow:0 10px 30px #000c;animation:authSlideIn .25s ease`;
    toast.innerHTML = `<strong style="display:block;margin-bottom:2px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:${color}">${error.code.replace(/_/g, ' ')}</strong><span>${error.message}</span>`;
    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; toast.style.transition = 'opacity .3s'; setTimeout(() => toast.remove(), 300); }, 5000);
  }

  function showBanner(error) {
    const container = ensureContainer('auth-banner-container');
    container.style.cssText += ';top:0;left:0;right:0;display:flex;justify-content:center';
    const banner = document.createElement('div');
    banner.style.cssText = 'pointer-events:auto;background:#3f1d1d;border-bottom:1px solid #7f2d2d;color:#fecaca;padding:10px 18px;width:100%;text-align:center;font-size:13px';
    banner.innerHTML = `<strong>${error.code.replace(/_/g, ' ')}:</strong> ${error.message}`;
    container.innerHTML = '';
    container.appendChild(banner);
  }

  function showModal(error) {
    const container = ensureContainer('auth-modal-container');
    container.style.cssText += ';inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;pointer-events:auto';
    const modal = document.createElement('div');
    modal.style.cssText = 'background:#0e1219;border:1px solid #314057;border-radius:14px;padding:24px;max-width:420px;width:calc(100vw - 48px);color:#e8eef7;box-shadow:0 24px 80px #000';
    modal.innerHTML = `<h2 style="margin:0 0 8px;font-size:16px;color:#f87171">Erro de autenticação</h2><p style="margin:0 0 18px;color:#b5c1d2">${error.message}</p><button id="auth-modal-ok" style="border:0;border-radius:9px;padding:10px 18px;background:#22d3ee;color:#031018;font-weight:700;cursor:pointer">Configurações</button>`;
    container.innerHTML = '';
    container.appendChild(modal);
    modal.querySelector('#auth-modal-ok').addEventListener('click', () => {
      container.remove();
      redirectToSettings(error.code);
    });
  }

  async function fetchWithAuth(url, options = {}) {
    const config = { ...DEFAULT_OPTIONS, ...options.authOptions };
    const handler = new AuthErrorHandler(config);
    const maxRetries = config.maxRetries;
    const timeoutMs = config.timeoutMs;

    let lastError = null;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);

      try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        clearTimeout(timer);

        if (response.ok) {
          return response;
        }

        let body = null;
        try {
          body = await response.json();
        } catch {
          body = null;
        }

        const error = handler.parse(response, body);

        if (error.requiresSettings) {
          handler.showFeedback(error);
          handler.redirectToSettings(error.code);
          throw Object.assign(new Error(error.message), { authError: error });
        }

        if (error.retryable && attempt < maxRetries) {
          const delay = Math.min(config.baseDelayMs * Math.pow(2, attempt), config.maxDelayMs);
          handler.showFeedback({ ...error, message: `${error.message} (tentativa ${attempt + 1}/${maxRetries + 1})` });
          await sleep(delay);
          continue;
        }

        handler.showFeedback(error);
        throw Object.assign(new Error(error.message), { authError: error });
      } catch (err) {
        clearTimeout(timer);
        if (err && err.authError) throw err;

        const code = err && err.name === 'AbortError' ? 'AUTH_TIMEOUT' : 'AUTH_NETWORK_ERROR';
        lastError = {
          code,
          status: 0,
          message: getErrorMessage(code),
          detail: err ? err.message : null,
          retryable: shouldRetry(code),
          requiresSettings: false,
          raw: null,
        };

        if (lastError.retryable && attempt < maxRetries) {
          const delay = Math.min(config.baseDelayMs * Math.pow(2, attempt), config.maxDelayMs);
          await sleep(delay);
          continue;
        }

        handler.showFeedback(lastError);
        throw Object.assign(new Error(lastError.message), { authError: lastError });
      }
    }

    throw lastError ? Object.assign(new Error(lastError.message), { authError: lastError }) : new Error('AUTH_UNKNOWN');
  }

  if (typeof document !== 'undefined' && !document.getElementById('auth-handler-styles')) {
    const style = document.createElement('style');
    style.id = 'auth-handler-styles';
    style.textContent = '@keyframes authSlideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}';
    document.head.appendChild(style);
  }

  return {
    AuthErrorHandler,
    handleApiError,
    getErrorMessage,
    shouldRetry,
    redirectToSettings,
    fetchWithAuth,
    ERROR_MESSAGES,
    RETRYABLE_CODES,
    SETTINGS_REDIRECT_CODES,
  };
});
