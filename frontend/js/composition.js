/*
 * Dark Studio - composition.js
 * Everything between "we have narration" and "we have an MP4": theme, caption
 * style, music, format, the POST /api/build-video call and an honest report of
 * what the renderer actually did.
 *
 * buildVideoFields() is the single mapping from UI state to the multipart
 * form the API expects, so no control can drift away from its API field.
 */

import {
  el, clear, card, badge, button, banner, emptyState, skeletonStack, kpi,
  toast, withBusy, definitionList, fmtDuration, statusDot,
} from './ui.js';
import * as api from './api.js';
import { state, setIn, patch } from './state.js';
import {
  loadPresets, ASPECT_OPTIONS, TRANSITION_OPTIONS, EFFECT_OPTIONS,
} from './catalog.js';
import { createPresetPicker } from './pickers/presets.js';
import { createSubtitleEditor } from './pickers/subtitle.js';
import { createMusicPicker } from './pickers/music.js';

/** The exact form /api/build-video reads. Nothing else is invented here. */
export function buildVideoFields({ projectName, topic, storyboard, style, music }) {
  const fields = {
    project_name: projectName,
    transition: state.project.transition,
    effect: state.project.effect,
    include_captions: String(Boolean(state.project.includeCaptions)),
    aspect_ratio: state.project.aspectRatio,
    topic: topic || '',
    preset: state.preset || '',
    subtitle_style: JSON.stringify(style),
    music_track: music.music_track,
    music_volume: String(music.music_volume),
    duck_voice: String(Boolean(music.duck_voice)),
  };
  if (storyboard && storyboard.length) fields.storyboard_json = JSON.stringify(storyboard);
  return fields;
}

/**
 * The render report. Zero scenes with media is shown as a warning, not hidden:
 * a silent 0 is the difference between "the search found nothing" and "we did
 * not look", and only the first one is a fact about this project.
 */
export function renderRenderSummary(result, ctx, { title = 'Resultado do render' } = {}) {
  if (!result) {
    return emptyState({
      title: 'Ainda nao houve render',
      text: 'Com guiao e narracao prontos, o botao de render produz o MP4 deste projeto.',
    });
  }
  const render = result.render_real || result.render || {};
  const ok = render.status === 'rendered' || result.status === 'rendered';
  const body = [];

  body.push(el('div', { class: 'row' }, [
    statusDot(ok ? 'ok' : 'err'),
    el('span', { class: 'grow' }, [
      el('span', { class: 'list-title', text: result.project_name || 'Projeto' }),
      el('span', { class: 'list-meta', text: render.message || render.error || 'Sem mensagem do servidor.' }),
    ]),
    badge(ok ? 'renderizado' : 'erro', ok ? 'ok' : 'err'),
  ]));

  const scenes = Number(result.scene_count ?? render.scene_count);
  const withMedia = Number(result.scenes_with_media ?? render.scenes_with_media);
  const grid = el('div', { class: 'grid-3' });
  grid.append(
    kpi('Cenas', Number.isFinite(scenes) ? String(scenes) : '--'),
    kpi('Cenas com imagem', Number.isFinite(withMedia) ? String(withMedia) : '--',
      withMedia === 0 ? 'warn' : 'ok'),
    kpi('Resolucao', render.resolution || '--'),
    kpi('Duracao', render.video_duration ? fmtDuration(render.video_duration) : '--'),
    kpi('Audio', render.audio_duration ? fmtDuration(render.audio_duration) : '--'),
    kpi('Palavras-chave', Array.isArray(result.keywords) ? String(result.keywords.length) : '--'),
  );
  body.push(grid);

  if (Number.isFinite(withMedia) && Number.isFinite(scenes) && withMedia < scenes) {
    body.push(banner('warn', 'Cenas sem imagem',
      `${scenes - withMedia} de ${scenes} cenas ficaram sem material visual. O motor usou apenas as imagens que conseguiu validar.`));
  }

  const rejected = Array.isArray(render.rejected_assets) ? render.rejected_assets : [];
  if (rejected.length) {
    const list = el('ul', { class: 'stack-2' });
    for (const item of rejected.slice(0, 8)) {
      const label = typeof item === 'string'
        ? item
        : `${item.reason || 'substituida'} . cena ${item.scene ?? '?'} . ${item.rejected_url || ''}`;
      list.appendChild(el('li', { class: 'small muted', text: label }));
    }
    body.push(el('details', {}, [
      el('summary', { class: 'small muted', text: `${rejected.length} recurso(s) rejeitado(s) pelo servidor` }),
      list,
    ]));
  }

  const music = result.music && typeof result.music === 'object' ? result.music : null;
  const facts = [
    ['Projeto', result.project_name],
    ['Tema aplicado', result.preset || render.preset],
    ['Fonte das palavras-chave', result.term_source],
    ['Motor', render.render_engine],
    ['Ficheiro', render.output_path],
    ['Musica aplicada', music ? (music.applied ? 'sim' : `nao. ${music.note || ''}`) : null],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '');

  if (facts.length) body.push(definitionList(facts));
  if (result.subtitle_style) {
    body.push(el('details', {}, [
      el('summary', { class: 'small muted', text: 'Estilo de legenda aplicado' }),
      el('pre', { class: 'code', text: JSON.stringify(result.subtitle_style, null, 2) }),
    ]));
  }

  if (ok && result.project_name) {
    const video = el('video', { class: 'player', controls: true, playsinline: true, attrs: { preload: 'metadata' } });
    video.src = api.projectVideoUrl(result.project_name);
    body.push(video);
    body.push(el('div', { class: 'row' }, [
      el('a', {
        class: 'btn btn-ghost btn-sm', target: '_blank', rel: 'noopener',
        href: api.projectVideoUrl(result.project_name), text: 'Abrir MP4',
      }),
      button({
        label: 'Editar cenas', variant: 'ghost', size: 'sm', iconName: 'film',
        onClick: () => ctx.navigate('studio', { project: result.project_name }),
      }),
    ]));
  }

  body.push(el('details', {}, [
    el('summary', { class: 'small muted', text: 'Resposta completa do servidor' }),
    el('pre', { class: 'code', text: JSON.stringify(result, null, 2) }),
  ]));

  return card({ title, body });
}

/** Composition controls plus the render action. Used by Criar and Estudio. */
export function createCompositionPanel(ctx, { topicSource } = {}) {
  const node = el('div', { class: 'stack-4' });
  const presetPicker = createPresetPicker({ compact: false, onChange: () => subtitleEditor.redraw() });
  const subtitleEditor = createSubtitleEditor();
  const musicPicker = createMusicPicker({ signal: ctx.signal });
  const formatRow = el('div', { class: 'choices choices-tight', role: 'group', 'aria-label': 'Formato' });
  const transitionRow = el('div', { class: 'chips', role: 'group', 'aria-label': 'Transicao' });
  const effectRow = el('div', { class: 'chips', role: 'group', 'aria-label': 'Efeito' });
  const captionsToggle = el('input', {
    id: 'comp-captions', type: 'checkbox',
    attrs: { checked: state.project.includeCaptions ? '' : null },
    on: { change: (event) => setIn('project', { includeCaptions: event.target.checked }) },
  });
  const resultBox = el('div', { class: 'stack-4' });
  const renderButton = button({ label: 'Renderizar MP4', iconName: 'film' });

  node.append(
    card({ title: 'Tema visual', body: [presetPicker.node, subtitleEditor.node] }),
    card({ title: 'Musica de fundo', body: [musicPicker.node] }),
    card({
      title: 'Formato e corte',
      body: [
        formatRow,
        el('div', { class: 'stack-2' }, [el('span', { class: 'section-title', text: 'Transicao' }), transitionRow]),
        el('div', { class: 'stack-2' }, [el('span', { class: 'section-title', text: 'Efeito' }), effectRow]),
        el('label', { class: 'check' }, [captionsToggle, el('span', { text: 'Incluir legendas' })]),
      ],
    }),
    card({ body: [renderButton, resultBox] }),
  );

  function drawChoices() {
    clear(formatRow);
    for (const option of ASPECT_OPTIONS) {
      formatRow.appendChild(el('button', {
        class: 'choice', type: 'button',
        'aria-pressed': state.project.aspectRatio === option.value ? 'true' : 'false',
        on: { click: () => { setIn('project', { aspectRatio: option.value }); drawChoices(); } },
      }, [
        el('span', { class: 'choice-title', text: option.label }),
        el('span', { class: 'choice-meta', text: option.meta }),
      ]));
    }
    clear(transitionRow);
    for (const option of TRANSITION_OPTIONS) {
      transitionRow.appendChild(el('button', {
        class: 'chip', type: 'button', text: option.label,
        'aria-pressed': state.project.transition === option.value ? 'true' : 'false',
        on: { click: () => { setIn('project', { transition: option.value }); drawChoices(); } },
      }));
    }
    clear(effectRow);
    for (const option of EFFECT_OPTIONS) {
      effectRow.appendChild(el('button', {
        class: 'chip', type: 'button', text: option.label,
        'aria-pressed': state.project.effect === option.value ? 'true' : 'false',
        on: { click: () => { setIn('project', { effect: option.value }); drawChoices(); } },
      }));
    }
  }

  async function refreshStoryboard() {
    if (state.storyboard.length) return state.storyboard;
    if (!state.segments.length) return [];
    return state.segments.map((segment, index) => ({
      index: index + 1,
      start: Number(segment.start) || 0,
      end: Number(segment.end) || 0,
      caption: String(segment.text || ''),
    }));
  }

  async function renderNow() {
    if (!state.project.name) {
      toast('Defina um nome de projeto.', 'warn');
      return;
    }
    clear(resultBox);
    resultBox.appendChild(skeletonStack(3));
    const storyboard = await refreshStoryboard();
    const fields = buildVideoFields({
      projectName: state.project.name,
      topic: topicSource ? topicSource() : state.project.topic,
      storyboard,
      style: subtitleEditor.value,
      music: musicPicker.value,
    });
    try {
      const result = await api.buildVideo(fields, ctx.signal);
      patch({ render: result });
      if (Array.isArray(result.storyboard) && result.storyboard.length) {
        patch({ storyboard: result.storyboard });
      }
      clear(resultBox);
      resultBox.appendChild(renderRenderSummary(result, ctx));
      toast('Render concluido.', 'ok');
    } catch (error) {
      if (error.cancelled) return;
      clear(resultBox);
      resultBox.appendChild(banner('err', 'O render nao terminou',
        error.message || 'O servidor nao devolveu um video.'));
    }
  }

  renderButton.addEventListener('click', () => withBusy(renderButton, renderNow));

  const ready = loadPresets(ctx.signal).catch((error) => {
    if (!error.cancelled) toast(error.message, 'err', 'Temas');
  });

  return {
    node,
    ready,
    presetPicker,
    subtitleEditor,
    musicPicker,
    renderRenderSummary: () => renderRenderSummary(state.render, ctx),
  };
}
