/*
 * Dark Studio - pickers/music.js
 * Background music for POST /api/build-video: `music_track`, `music_volume`
 * and `duck_voice`. Mood chips and the track list come from
 * /api/music/tracks, free-text search from /api/music/search and new beds from
 * /api/music/upload. An empty library is shown as an empty state, never as a
 * fabricated track.
 */

import {
  el, clear, button, toast, withBusy, skeletonStack, emptyState, fmtDuration, badge,
} from '../ui.js';
import { state, setIn } from '../state.js';
import * as api from '../api.js';
import { loadMoods } from '../catalog.js';

export function createMusicPicker({ signal }) {
  const node = el('div', { class: 'stack-4' });
  const moodsRow = el('div', { class: 'chips', role: 'group', 'aria-label': 'Ambiente musical' });
  const listNode = el('div', { class: 'list' });
  const detailNode = el('div', { class: 'row' });
  node.appendChild(el('div', { class: 'stack-2' }, [
    el('span', { class: 'section-title', text: 'Ambiente' }), moodsRow,
  ]));
  node.appendChild(listNode);
  node.appendChild(detailNode);

  let tracks = [];
  let searchTerm = '';

  async function loadTracks() {
    clear(listNode);
    listNode.appendChild(skeletonStack(2, true));
    try {
      const data = await api.getMusicTracks(state.music.mood, signal);
      tracks = Array.isArray(data.tracks) ? data.tracks : [];
      renderTracks();
    } catch (error) {
      if (error.cancelled) return;
      clear(listNode);
      listNode.appendChild(emptyState({
        title: 'Biblioteca musical indisponivel',
        text: error.message || 'Nao foi possivel ler as faixas.',
        actionLabel: 'Tentar de novo',
        onAction: loadTracks,
      }));
    }
  }

  function drawMoods() {
    clear(moodsRow);
    for (const mood of state.catalog.moods) {
      moodsRow.appendChild(el('button', {
        class: 'chip',
        type: 'button',
        'aria-pressed': mood === state.music.mood ? 'true' : 'false',
        text: mood,
        on: {
          click: () => {
            setIn('music', { mood, track: '' });
            drawMoods();
            loadTracks();
          },
        },
      }));
    }
  }

  function drawDetail() {
    clear(detailNode);
    const volume = el('input', {
      class: 'range',
      id: 'music-volume',
      type: 'range',
      min: 0,
      max: 1,
      step: 0.01,
      value: state.music.volume,
      on: { input: (event) => setIn('music', { volume: Number(event.target.value) }) },
    });
    const volumeOut = el('span', {
      class: 'range-value',
      text: `${Math.round(Number(state.music.volume) * 100)}%`,
    });
    volume.addEventListener('input', () => {
      volumeOut.textContent = `${Math.round(Number(volume.value) * 100)}%`;
    });
    const duck = el('input', {
      id: 'music-duck',
      type: 'checkbox',
      attrs: { checked: state.music.duckVoice ? '' : null },
      on: { change: (event) => setIn('music', { duckVoice: event.target.checked }) },
    });
    const upload = el('input', {
      class: 'file-input',
      id: 'music-upload',
      type: 'file',
      accept: 'audio/*',
    });
    upload.addEventListener('change', () => {
      const file = upload.files && upload.files[0];
      if (!file) return;
      withBusy(upload, async () => {
        try {
          const data = await api.uploadMusic(file, file.name, state.music.mood, signal);
          const track = data && data.track;
          if (track && track.id) {
            setIn('music', { track: track.id });
            toast(`Faixa "${track.title || track.id}" registada.`, 'ok');
            await loadTracks();
          } else {
            toast('O servidor aceitou o ficheiro mas nao devolveu uma faixa.', 'warn');
          }
        } catch (error) {
          if (!error.cancelled) toast(error.message, 'err', 'Falha no envio');
        }
      });
    });

    detailNode.appendChild(el('div', { class: 'field grow' }, [
      el('div', { class: 'row row-end' }, [
        el('label', { attrs: { for: 'music-volume' }, text: 'Volume da musica' }), volumeOut,
      ]),
      volume,
    ]));
    detailNode.appendChild(el('label', { class: 'check' }, [duck, el('span', { text: 'Baixar a musica com a voz' })]));
    detailNode.appendChild(el('div', { class: 'field grow' }, [
      el('label', { attrs: { for: 'music-upload' }, text: 'Carregar faixa' }), upload,
    ]));
  }

  function renderTracks() {
    clear(listNode);
    if (!tracks.length) {
      listNode.appendChild(emptyState({
        title: searchTerm ? 'Nenhum resultado' : 'Nenhuma faixa neste ambiente',
        text: searchTerm
          ? `Nada corresponde a "${searchTerm}".`
          : 'Carregue um ficheiro de audio para usar musica de fundo.',
      }));
      return;
    }
    for (const track of tracks) {
      const active = track.id === state.music.track;
      listNode.appendChild(el('button', {
        class: 'list-row',
        type: 'button',
        'aria-pressed': active ? 'true' : 'false',
        style: active ? 'border-color:var(--accent-line)' : '',
        on: { click: () => { setIn('music', { track: track.id }); renderTracks(); } },
      }, [
        el('span', { class: 'grow' }, [
          el('span', { class: 'list-title', text: track.title || track.id }),
          el('span', { class: 'list-meta', text: `${track.mood || 'sem ambiente'} . ${track.source || 'desconhecido'}` }),
        ]),
        badge(fmtDuration(track.duration), active ? 'ok' : ''),
      ]));
    }
  }

  async function boot() {
    try {
      await loadMoods(signal);
    } catch (error) {
      if (!error.cancelled) toast(error.message, 'err', 'Ambientes');
    }
    drawMoods();
    await loadTracks();
    drawDetail();
  }

  boot();

  return {
    node,
    reload: loadTracks,
    get value() {
      return {
        music_track: state.music.track || '',
        music_volume: Number(state.music.volume),
        duck_voice: Boolean(state.music.duckVoice),
      };
    },
  };
}
