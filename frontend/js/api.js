/*
 * Dark Studio - api.js
 * The single network seam. Every request goes through apiFetch(), which
 * normalises transport failures and routes provider errors through
 * auth-handler.js (window.AuthHandler), so the shared AUTH_* contract, the
 * retry flags and the settings redirect behave the same everywhere.
 *
 * The base is a relative empty string: the page is served from the same origin
 * as the API (mounted at /app/), so requests resolve to /api/... on whatever
 * port the launcher picked, with no CORS preflight and no hardcoded host.
 */

const API_BASE = '';

const inFlight = new Set();
let redirectHandler = null;

/** Called by auth-handler when a key is missing or rejected. */
export function onAuthRedirect(handler) {
  redirectHandler = handler;
}

function authHandler() {
  if (!window.AuthHandler) return null;
  return new window.AuthHandler.AuthErrorHandler({
    feedback: 'toast',
    onRedirect: (code) => { if (redirectHandler) redirectHandler(code); },
  });
}

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', detail = null, body = null, cancelled = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.body = body;
    this.cancelled = cancelled;
  }
}

export const isCancelled = (error) => Boolean(error && error.cancelled);

/** A signal bound to the current view; aborted by abortAll() on navigation. */
export function createSignal() {
  const controller = new AbortController();
  inFlight.add(controller);
  return {
    signal: controller.signal,
    dispose() { inFlight.delete(controller); controller.abort(); },
  };
}

/** Abort everything still in flight, e.g. when the user leaves a view. */
export function abortAll() {
  for (const controller of Array.from(inFlight)) {
    try { controller.abort(); } catch { /* already gone */ }
    inFlight.delete(controller);
  }
}

async function errorBody(response) {
  try { return await response.json(); } catch { return null; }
}

function pickDetail(body) {
  if (!body) return null;
  if (typeof body === 'string') return body.slice(0, 300);
  const candidate = body.detail ?? body.error ?? body.message ?? body.fallback_error;
  if (typeof candidate === 'string') return candidate;
  if (body.errors) return JSON.stringify(body.errors).slice(0, 300);
  return null;
}

/**
 * Fetch plus error normalisation. Resolves to the raw Response on success.
 * Pass { silent: true } for probes whose failure is an expected answer rather
 * than an error worth interrupting the user for.
 */
export async function apiFetch(path, { method = 'GET', json, form, signal, silent = false } = {}) {
  const controller = new AbortController();
  inFlight.add(controller);
  const forwardAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', forwardAbort, { once: true });
  }

  const init = { method, signal: controller.signal };
  if (json !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(json);
  } else if (form !== undefined) {
    init.body = form;
  }

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch (error) {
    if (signal && signal.aborted) throw new ApiError('Pedido cancelado.', { cancelled: true });
    const code = 'AUTH_NETWORK_ERROR';
    const message = window.AuthHandler
      ? window.AuthHandler.getErrorMessage(code)
      : 'Sem ligacao ao servidor.';
    const handler = authHandler();
    if (!silent && handler) {
      handler.showFeedback({
        code, status: 0, message, detail: String(error),
        retryable: true, requiresSettings: false, raw: null,
      });
    }
    throw new ApiError(message, { code, detail: String(error) });
  } finally {
    inFlight.delete(controller);
    if (signal) signal.removeEventListener('abort', forwardAbort);
  }

  if (response.ok) return response;

  const body = await errorBody(response);
  const handler = authHandler();
  let code = '';
  let message = '';
  if (handler) {
    const error = handler.parse(response, body);
    code = error.code;
    message = error.message;
    if (!silent) {
      handler.showFeedback(error);
      if (error.requiresSettings) handler.redirectToSettings(error.code);
    }
  }
  const detail = pickDetail(body);
  throw new ApiError(message || detail || `Erro ${response.status}.`, {
    status: response.status, code, detail, body,
  });
}

export async function apiJson(path, options) {
  const response = await apiFetch(path, options);
  return response.json();
}

function asForm(fields) {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null) continue;
    form.append(key, String(value));
  }
  return form;
}

/* ------------------------------------------------------------- endpoints */

export const getHealth = (signal) => apiJson('/health', { signal });
export const getProviders = (signal) => apiJson('/api/providers', { signal });
export const getLanguages = (signal) => apiJson('/api/languages', { signal });
export const getVoices = (locale = '', signal) =>
  apiJson(`/api/voices${locale ? `?locale=${encodeURIComponent(locale)}` : ''}`, { signal });
export const getTtsStatus = (signal) => apiJson('/api/tts/status', { signal });
export const getPresets = (signal) => apiJson('/api/presets', { signal });
export const getMusicTracks = (mood = '', signal) =>
  apiJson(`/api/music/tracks${mood ? `?mood=${encodeURIComponent(mood)}` : ''}`, { signal });
export const searchMusic = (q, signal) =>
  apiJson(`/api/music/search?q=${encodeURIComponent(q)}`, { signal });
export const listProjects = (signal) => apiJson('/api/projects', { signal });
export const getHighlights = (name, signal) =>
  apiJson(`/api/shorts/${encodeURIComponent(name)}/highlights`, { signal });
export const searchMedia = (text, provider = 'pexels', signal) =>
  apiJson(
    `/api/media/search?query=${encodeURIComponent(text)}&provider=${encodeURIComponent(provider)}`,
    { signal },
  );
export const getGenerateJob = (jobId, signal) =>
  apiJson(`/api/jobs/${encodeURIComponent(jobId)}`, { signal, silent: true });

/**
 * Job history. The one endpoint that answers with a bare JSON array instead of
 * an envelope object, so a caller must not read a `.jobs` key off the result.
 */
export const listJobs = (signal) => apiJson('/api/jobs', { signal, silent: true });

export const createScript = (payload, signal) =>
  apiJson('/api/script', { method: 'POST', json: payload, signal });
export const synthesize = (payload, signal) =>
  apiJson('/api/tts', { method: 'POST', json: payload, signal });
export const buildVideo = (fields, signal) =>
  apiJson('/api/build-video', { method: 'POST', form: asForm(fields), signal });
export const buildViral = (fields, signal) =>
  apiJson('/api/build-viral', { method: 'POST', form: asForm(fields), signal });
export const buildShorts = (fields, signal) =>
  apiJson('/api/shorts', { method: 'POST', form: asForm(fields), signal });
export const getStrategy = (payload, signal) =>
  apiJson('/api/strategy', { method: 'POST', json: payload, signal });
export const saveSettings = (payload, signal) =>
  apiJson('/api/settings', { method: 'POST', json: payload, signal });
export const testAiProvider = (signal) =>
  apiJson('/api/ai/test', { method: 'POST', signal });
export const uploadMusic = (file, title, mood, signal) => {
  const form = new FormData();
  form.append('file', file);
  form.append('title', title || '');
  form.append('mood', mood || 'ambient');
  return apiJson('/api/music/upload', { method: 'POST', form, signal });
};
export const transcribeAudio = (file, projectName, signal) => {
  const form = new FormData();
  form.append('file', file);
  form.append('project_name', projectName);
  return apiJson('/api/transcribe', { method: 'POST', form, signal });
};

/**
 * One-click pipeline. 202 with a job id when the orchestrator is mounted; a
 * 404 means this server build has no such route and the caller falls back to
 * the manual script -> TTS -> render flow.
 */
export const startGenerate = (payload, signal) =>
  apiJson('/api/generate', { method: 'POST', json: payload, signal });

/* ------------------------------------------------------------------ media */

export const projectVideoUrl = (name, bust = true) =>
  `${API_BASE}/api/project/${encodeURIComponent(name)}/video${bust ? `?t=${Date.now()}` : ''}`;

export const shortsVideoUrl = (name, bust = true) =>
  `${API_BASE}/api/project/${encodeURIComponent(name)}/shorts/video${bust ? `?t=${Date.now()}` : ''}`;

/**
 * Does this project have a rendered MP4? A HEAD against the same route the
 * <video> element uses: 200 means rendered, 404 means never generated. Silent
 * on purpose - a 404 here is the answer, not a failure to report.
 */
export async function hasRenderedVideo(name, signal) {
  try {
    const response = await fetch(projectVideoUrl(name, false), { method: 'HEAD', signal });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Is POST /api/generate mounted on this server build? A HEAD is enough: a
 * route that exists but only accepts POST answers 405, a route that does not
 * exist answers 404. Used to tell the user whether one-click generation is
 * available instead of failing after they press the button.
 */
export async function probeGenerateRoute(signal) {
  try {
    const response = await fetch(`${API_BASE}/api/generate`, { method: 'HEAD', signal });
    return response.ok || response.status === 405;
  } catch {
    return false;
  }
}
