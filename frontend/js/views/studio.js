/*
 * Dark Studio - views/studio.js
 * The timeline editor. It edits the scenes the session already holds - the ones
 * a transcription or a render produced - and posts them back as the
 * `storyboard_json` of POST /api/build-video.
 *
 * The API exposes no route that returns the scenes of a stored project, so
 * this screen says exactly that instead of pretending it can load them.
 */

import {
  el, clear, card, button, banner, emptyState, pageHead, toast, withBusy,
  skeletonStack, definitionList, statusDot, fmtDuration,
} from '../ui.js';
import { state, setIn, patch } from '../state.js';
import { EFFECT_OPTIONS } from '../catalog.js';
import { buildVideoFields, renderRenderSummary } from '../composition.js';
import * as api from '../api.js';

const FORMATS = [
  { value: 'vertical', label: 'Vertical 9:16' },
  { value: 'square', label: 'Quadrado 1:1' },
  { value: 'landscape', label: 'Horizontal 16:9' },
];

export async function render(root, ctx) {
  root.appendChild(pageHead(
    'Estudio',
    'Linha de tempo',
    'Ajuste texto, efeito e imagens de cada cena antes de renderizar.',
    [button({ label: 'Projetos', variant: 'ghost', iconName: 'layers', onClick: () => ctx.navigate('projects') })],
  ));

  if (ctx.params && ctx.params.project) setIn('project', { name: ctx.params.project });

  const sceneHost = el('div', { class: 'stack-2' }, [skeletonStack(2, true)]);
  const resultHost = el('div', {});
  const controls = el('div', { class: 'stack-4' });
  const applyButton = button({ label: 'Aplicar cenas e renderizar', iconName: 'film' });
  applyButton.classList.add('btn-lg');

  root.append(
    card({
      title: 'Cenas',
      subtitle: 'Cada linha e uma cena do storyboard que sera enviado ao renderizador.',
      body: [
        el('div', { class: 'row' }, [
          el('span', { class: 'section-title', text: 'Projeto' }),
          el('span', { class: 'mono', text: state.project.name }),
        ]),
        sceneHost,
      ],
    }),
    card({ title: 'Composicao', body: [controls] }),
    resultHost,
  );

  function drawControls() {
    clear(controls);
    const formats = el('div', { class: 'choices choices-tight', role: 'group', 'aria-label': 'Formato' });
    for (const option of FORMATS) {
      formats.appendChild(el('button', {
        class: 'choice', type: 'button', text: option.label,
        'aria-pressed': state.project.aspectRatio === option.value ? 'true' : 'false',
        on: { click: () => { setIn('project', { aspectRatio: option.value }); drawControls(); } },
      }));
    }
    const scenes = state.storyboard;
    controls.append(
      formats,
      definitionList([
        ['Cenas', scenes.length],
        ['Com imagem', scenes.filter((scene) => scene.media_url).length],
        ['Tema', state.preset],
        ['Transicao', state.project.transition],
        ['Efeito', state.project.effect],
        ['Legendas', state.project.includeCaptions ? 'sim' : 'nao'],
        ['Musica', state.music.track || 'nenhuma'],
      ]),
      el('p', { class: 'hint', text:
        'O tema, as legendas e a musica sao os que definiu em "Criar". Aqui pode corrigir as cenas e o formato antes de renderizar.' }),
      applyButton,
    );
    applyButton.disabled = scenes.length === 0;
  }

  function scenePayload() {
    return state.storyboard.map((scene, index) => ({
      index: index + 1,
      start: Number(scene.start) || 0,
      end: Number(scene.end) || 0,
      duration: Math.max(0, (Number(scene.end) || 0) - (Number(scene.start) || 0)),
      caption: String(scene.caption || scene.headline || ''),
      effect: scene.effect || state.project.effect,
      transition: scene.transition || state.project.transition,
      tone: scene.tone || 'narrative',
      media_url: scene.media_url || '',
      search_terms: Array.isArray(scene.search_terms) ? scene.search_terms : [],
    }));
  }

  function drawScenes() {
    clear(sceneHost);
    if (!state.storyboard.length) {
      sceneHost.appendChild(emptyState({
        title: 'Nenhuma cena carregada',
        text: 'A API nao tem rota para ler as cenas de um projeto guardado. Gere um guiao, transcreva um audio ou faca um render para as cenas chegarem aqui.',
        actionLabel: 'Criar conteudo',
        onAction: () => ctx.navigate('create'),
      }));
      return;
    }
    const list = el('div', { class: 'scenes' });
    state.storyboard.forEach((scene, index) => list.appendChild(sceneRow(scene, index)));
    sceneHost.appendChild(list);
    const withMedia = state.storyboard.filter((scene) => scene.media_url).length;
    sceneHost.appendChild(el('p', { class: 'hint', text:
      `${state.storyboard.length} cena(s), ${withMedia} com imagem. As cenas sem imagem usam o fundo do tema.` }));
  }

  function sceneRow(scene, index) {
    const caption = el('textarea', { class: 'textarea', rows: 2 });
    caption.value = String(scene.caption || scene.headline || '');
    caption.setAttribute('aria-label', `Legenda da cena ${index + 1}`);
    caption.addEventListener('input', (event) => {
      state.storyboard[index].caption = event.target.value;
    });

    const start = numberInput(`Inicio da cena ${index + 1}`, scene.start);
    const end = numberInput(`Fim da cena ${index + 1}`, scene.end);
    const duration = el('span', { class: 'scene-time' });
    const sync = () => {
      state.storyboard[index].start = Number(start.value) || 0;
      state.storyboard[index].end = Number(end.value) || 0;
      duration.textContent = fmtDuration((Number(end.value) || 0) - (Number(start.value) || 0));
    };
    start.addEventListener('change', sync);
    end.addEventListener('change', sync);
    duration.textContent = fmtDuration((Number(scene.end) || 0) - (Number(scene.start) || 0));

    const effect = el('select', { class: 'select' });
    effect.setAttribute('aria-label', `Efeito da cena ${index + 1}`);
    for (const option of EFFECT_OPTIONS) {
      const node = el('option', { value: option.value, text: option.label });
      if (option.value === (scene.effect || state.project.effect)) node.selected = true;
      effect.appendChild(node);
    }
    effect.addEventListener('change', (event) => { state.storyboard[index].effect = event.target.value; });

    const thumb = el('div', { class: 'scene-thumb' }, [el('span', { text: scene.media_url ? '' : 'sem imagem' })]);
    if (scene.media_url) thumb.style.backgroundImage = `url("${encodeURI(String(scene.media_url))}")`;

    return el('div', { class: 'scene' }, [
      el('span', { class: 'scene-index', text: String(index + 1).padStart(2, '0') }),
      thumb,
      el('div', { class: 'scene-fields' }, [
        caption,
        el('div', { class: 'row' }, [
          el('span', { class: 'section-title', text: 'Inicio' }), start,
          el('span', { class: 'section-title', text: 'Fim' }), end,
          duration,
        ]),
      ]),
      el('div', { class: 'stack-2' }, [
        el('span', { class: 'section-title', text: 'Efeito' }), effect,
        statusDot(scene.media_url ? 'ok' : 'warn'),
      ]),
    ]);
  }

  function numberInput(label, value) {
    const input = el('input', {
      class: 'input', type: 'number', step: '0.1', min: '0',
      value: String(Number(value) || 0), attrs: { 'aria-label': label },
    });
    return input;
  }

  applyButton.addEventListener('click', () => withBusy(applyButton, async () => {
    clear(resultHost);
    resultHost.appendChild(skeletonStack(2));
    try {
      const result = await api.buildVideo(buildVideoFields({
        projectName: state.project.name,
        topic: state.project.topic,
        storyboard: scenePayload(),
        style: state.style,
        music: {
          music_track: state.music.track || '',
          music_volume: Number(state.music.volume),
          duck_voice: Boolean(state.music.duckVoice),
        },
      }), ctx.signal);
      patch({ render: result });
      if (Array.isArray(result.storyboard) && result.storyboard.length) {
        patch({ storyboard: result.storyboard });
      }
      clear(resultHost);
      resultHost.appendChild(renderRenderSummary(result, ctx, { title: 'Render apos as alteracoes' }));
      toast('Render concluido.', 'ok');
      drawScenes();
      drawControls();
    } catch (error) {
      if (error.cancelled) return;
      clear(resultHost);
      resultHost.appendChild(banner('err', 'O render nao terminou', error.message));
    }
  }));

  drawScenes();
  drawControls();
  return {};
}
