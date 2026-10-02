/*
 * Dark Studio - catalog.js
 * Lazily loads and caches the reference data the views share (theme presets,
 * caption vocabularies, voices, languages, music moods) so two screens never
 * fetch the same list twice. A failure propagates to the caller and leaves the
 * cache untouched, so a retry can still succeed.
 */

import * as api from './api.js';
import { patch, state } from './state.js';

const loaded = { presets: false, languages: false, moods: false, voices: '' };

export async function loadPresets(signal) {
  if (loaded.presets) return state.catalog;
  const data = await api.getPresets(signal);
  patch({
    catalog: {
      ...state.catalog,
      presets: Array.isArray(data.presets) ? data.presets : [],
      positions: Array.isArray(data.positions) ? data.positions : [],
      modes: Array.isArray(data.modes) ? data.modes : [],
      fonts: Array.isArray(data.fonts) ? data.fonts : [],
    },
  });
  loaded.presets = true;
  return state.catalog;
}

export async function loadLanguages(signal) {
  if (loaded.languages) return state.catalog;
  const data = await api.getLanguages(signal);
  patch({
    catalog: { ...state.catalog, languages: Array.isArray(data.languages) ? data.languages : [] },
  });
  loaded.languages = true;
  return state.catalog;
}

export async function loadMoods(signal) {
  if (loaded.moods) return state.catalog;
  const data = await api.getMusicTracks('', signal);
  patch({
    catalog: { ...state.catalog, moods: Array.isArray(data.moods) ? data.moods : [] },
  });
  loaded.moods = true;
  return state.catalog;
}

export async function loadVoices(locale = '', signal) {
  if (loaded.voices === locale && state.catalog.voices.length) return state.catalog;
  const data = await api.getVoices(locale, signal);
  patch({
    catalog: { ...state.catalog, voices: Array.isArray(data.voices) ? data.voices : [] },
  });
  loaded.voices = locale;
  return state.catalog;
}

export function invalidateVoices() {
  loaded.voices = '';
}

/** Voices grouped by locale, in catalogue order, for the picker. */
export function groupVoicesByLocale(voices = state.catalog.voices) {
  const groups = new Map();
  for (const voice of voices) {
    const locale = voice.locale || 'outros';
    if (!groups.has(locale)) groups.set(locale, []);
    groups.get(locale).push(voice);
  }
  return Array.from(groups, ([locale, items]) => ({ locale, items }));
}

export const TONE_OPTIONS = [
  { value: 'documentary', label: 'Documentario' },
  { value: 'educational', label: 'Educativo' },
  { value: 'conversational', label: 'Conversacional' },
  { value: 'dramatic', label: 'Dramatico' },
  { value: 'energetic', label: 'Energetico' },
  { value: 'calm', label: 'Calmo' },
  { value: 'investigative', label: 'Investigativo' },
];

export const ASPECT_OPTIONS = [
  { value: 'vertical', label: 'Vertical 9:16', meta: 'Reels, TikTok, Shorts' },
  { value: 'square', label: 'Quadrado 1:1', meta: 'Feed do Instagram' },
  { value: 'landscape', label: 'Horizontal 16:9', meta: 'YouTube' },
];

export const TRANSITION_OPTIONS = [
  { value: 'fade', label: 'Fade' },
  { value: 'wipe', label: 'Wipe' },
  { value: 'slide_left', label: 'Deslizar' },
  { value: 'cut', label: 'Corte' },
];

export const EFFECT_OPTIONS = [
  { value: 'cinematic', label: 'Cinematic' },
  { value: 'glow', label: 'Glow' },
  { value: 'clean', label: 'Clean' },
];
