/*
 * Dark Studio - projects.js
 * /api/projects lists raw upload files, not projects. This module folds those
 * files into one row per project and probes the render route so the UI can
 * honestly separate "rendered", "never generated" and "no captions yet"
 * instead of guessing from a filename.
 */

import * as api from './api.js';

const AUDIO = /\.(mp3|wav|m4a|aac|ogg|flac)$/i;

export function foldProjects(files) {
  const byName = new Map();
  for (const file of Array.isArray(files) ? files : []) {
    const raw = String(file && file.name ? file.name : '');
    if (!raw) continue;
    const cut = raw.lastIndexOf('.');
    if (cut <= 0) continue;
    const name = raw.slice(0, cut);
    const suffix = raw.slice(cut).toLowerCase();
    if (!byName.has(name)) byName.set(name, { name, assets: [] });
    const entry = byName.get(name);
    entry.assets.push(raw);
    if (suffix === '.srt') entry.hasSrt = true;
    else if (suffix === '.script.json') entry.hasScript = true;
    else if (AUDIO.test(suffix)) entry.hasAudio = true;
  }
  return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name));
}

export async function loadProjectIndex(signal) {
  const data = await api.listProjects(signal);
  const rows = foldProjects(data && data.projects);
  await Promise.all(rows.map(async (row) => {
    row.rendered = row.hasSrt ? await api.hasRenderedVideo(row.name, signal) : false;
  }));
  return rows;
}

/**
 * A project can only be rendered when its SRT exists: build-video refuses
 * without captions, and TTS/transcribe are what write that file.
 */
export function projectState(row) {
  if (!row.hasSrt) {
    return { key: 'sem-legendas', label: 'Sem legenda', tone: 'warn', detail: 'Falta gerar ou transcrever a narração.' };
  }
  if (row.rendered) {
    return { key: 'renderizado', label: 'Renderizado', tone: 'ok', detail: 'MP4 disponivel para projecao.' };
  }
  return { key: 'nao-gerado', label: 'Nao gerado', tone: '', detail: 'Legendas prontas, ainda sem render.' };
}
