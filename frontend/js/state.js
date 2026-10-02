/*
 * Dark Studio - state.js
 * A tiny observable store. Views read slices of `state` and subscribe to learn
 * what changed; nothing else mutates it. Preferences (the fields the user
 * would be annoyed to retype) persist in localStorage. Server data does not:
 * it goes stale, and the UI must never show a fabricated number.
 */

const STORAGE_KEY = 'darkstudio.prefs.v1';

export const DEFAULT_STYLE = {
  font_family: 'Inter',
  font_size: 52,
  primary_color: '#FFFFFF',
  stroke_color: '#000000',
  stroke_width: 4,
  shadow: true,
  background_opacity: 0,
  background_color: '#05070B',
  position: 'bottom',
  uppercase: false,
  bold: true,
  max_width_percent: 84,
  mode: 'bottom',
};

const initial = {
  route: 'dashboard',
  project: {
    name: 'meu_documentario',
    topic: '',
    niche: '',
    language: 'pt-PT',
    tone: 'documentary',
    sectionCount: 5,
    durationTarget: 60,
    customInstructions: '',
    aspectRatio: 'vertical',
    transition: 'fade',
    effect: 'cinematic',
    includeCaptions: true,
  },
  voice: { provider: 'edge', id: '', rate: '+0%', pitch: '+0Hz' },
  style: { ...DEFAULT_STYLE },
  preset: 'cinematic',
  music: { mood: 'ambient', track: '', volume: 0.18, duckVoice: true },
  catalog: { presets: [], positions: [], modes: [], fonts: [], voices: [], languages: [], moods: [] },
  capabilities: null,
  script: null,
  segments: [],
  storyboard: [],
  render: null,
  shorts: null,
  generate: null,
  projects: [],
};

export const state = load();

function load() {
  const base = structuredClone(initial);
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    for (const key of ['project', 'voice', 'style', 'music', 'preset']) {
      if (saved[key] && typeof saved[key] === 'object') {
        base[key] = { ...base[key], ...saved[key] };
      }
    }
  } catch {
    // A corrupt or blocked store must never stop the app from booting.
  }
  return base;
}

const PERSISTED = ['project', 'voice', 'style', 'music', 'preset'];
const listeners = new Set();

export function persist() {
  try {
    const payload = {};
    for (const key of PERSISTED) payload[key] = state[key];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Private mode or a full quota: preferences simply do not survive.
  }
}

export function patch(partial, { save = true } = {}) {
  const changed = [];
  for (const [key, value] of Object.entries(partial)) {
    if (state[key] === value) continue;
    state[key] = value;
    changed.push(key);
  }
  if (!changed.length) return state;
  if (save && changed.some((key) => PERSISTED.includes(key))) persist();
  for (const listener of listeners) {
    try {
      listener(state, changed);
    } catch (error) {
      console.error('Falha num subscritor do estado:', error);
    }
  }
  return state;
}

export function setIn(section, partial) {
  return patch({ [section]: { ...state[section], ...partial } });
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Merge a preset's own look into the style editor, keeping user overrides. */
export function applyPresetStyle(subtitle) {
  if (!subtitle || typeof subtitle !== 'object') return state.style;
  return setIn('style', { ...DEFAULT_STYLE, ...subtitle });
}
