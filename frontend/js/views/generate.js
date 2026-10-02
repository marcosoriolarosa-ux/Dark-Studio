/*
 * Dark Studio - views/generate.js
 * The headline path: a topic goes in, a finished video comes out.
 *
 * The form only collects the payload; the job contract (202 + polling) lives
 * in ../generator.js. When POST /api/generate is not mounted on this server
 * build the screen says so plainly and hands off to the manual flow rather
 * than leaving a dead button behind.
 */

import {
  el, clear, card, button, banner, pageHead, field, textareaField,
  selectField, toast, withBusy,
} from '../ui.js';
import * as api from '../api.js';
import { state, setIn } from '../state.js';
import { loadLanguages, loadVoices, loadPresets, loadMoods, ASPECT_OPTIONS } from '../catalog.js';
import { createPresetPicker } from '../pickers/presets.js';
import { createGeneratorPanel } from '../generator.js';

export async function render(root, ctx) {
  root.appendChild(pageHead(
    'Gerar',
    'Um tema entra, um video sai',
    'O orquestrador escreve o guiao, narra, procura imagens e renderiza. Voce ve cada etapa.',
  ));

  const formColumn = el('div', { class: 'stack-4' });
  const noticeSlot = el('div', { class: 'stack-2' });
  let available = true;

  const panel = createGeneratorPanel(ctx, {
    onUnavailable: () => {
      available = false;
      runButton.disabled = true;
      noticeSlot.replaceChildren(
        banner('warn', 'Geracao automatica indisponivel neste servidor',
          'POST /api/generate ainda nao esta montado. O fluxo manual usa os mesmos campos, passo a passo.'),
        manualButton(),
      );
    },
  });

  const formCard = card({ title: 'Briefing', body: [] });
  const styleCard = card({
    title: 'Tema visual',
    subtitle: 'Cada tema traz o seu proprio estilo de legenda e paleta.',
    body: [],
  });
  const musicCard = card({
    title: 'Ambiente musical',
    subtitle: 'A pista escolhida entra em baixo da voz.',
    body: [],
  });

  const runButton = button({ label: 'Gerar video completo', iconName: 'spark' });
  runButton.classList.add('btn-lg');
  const manualButton = () => button({
    label: 'Continuar no fluxo manual',
    variant: 'ghost',
    iconName: 'sliders',
    onClick: () => ctx.navigate('create', { topic: topic.input.value }),
  });

  formColumn.append(formCard, styleCard, musicCard, el('div', { class: 'stack-2' }, [noticeSlot, runButton]));
  root.appendChild(el('div', { class: 'split' }, [formColumn, panel.node]));

  // ------------------------------------------------------------- the form
  const topic = textareaField({
    label: 'Tema do video', id: 'gen-topic', rows: 3,
    value: state.project.topic,
    placeholder: 'Ex.: a historia invisivel dos cabos submarinos',
    hint: 'Uma frase basta. O modelo escreve o resto.',
  });
  topic.input.addEventListener('input', (event) => {
    topic.input.removeAttribute('aria-invalid');
    setIn('project', { topic: event.target.value });
  });

  const projectName = field({
    label: 'Nome do projeto', id: 'gen-project', value: state.project.name,
    hint: 'O servidor remove acentos e simbolos deste nome.',
  });
  projectName.input.addEventListener('input', (event) => setIn('project', { name: event.target.value }));

  const sections = field({
    label: 'Numero de seccoes', id: 'gen-sections', type: 'number',
    value: String(state.project.sectionCount), attrs: { min: 1, max: 20 },
  });

  const language = selectField({
    label: 'Idioma da narracao', id: 'gen-language',
    options: [{ value: state.project.language, label: state.project.language }],
  });

  const voiceSelect = selectField({
    label: 'Voz', id: 'gen-voice',
    options: [{ value: '', label: 'A carregar...' }],
  });

  const captions = el('input', {
    id: 'gen-captions', type: 'checkbox',
    attrs: { checked: state.project.includeCaptions ? '' : null },
    on: { change: (event) => setIn('project', { includeCaptions: event.target.checked }) },
  });

  formCard.body.append(
    topic.node,
    el('div', { class: 'grid-2' }, [projectName.node, sections.node]),
    el('div', { class: 'grid-2' }, [language.node, voiceSelect.node]),
    el('label', { class: 'check' }, [captions, el('span', { text: 'Incluir legendas no render' })]),
  );

  const presetPicker = createPresetPicker({ compact: true });
  const aspectOptions = el('div', { class: 'choices choices-tight', role: 'group', 'aria-label': 'Formato' });
  styleCard.body.append(presetPicker.node, aspectOptions);

  const moodRow = el('div', { class: 'chips', role: 'group', 'aria-label': 'Ambiente musical' });
  musicCard.body.append(moodRow);

  function drawAspect() {
    clear(aspectOptions);
    for (const option of ASPECT_OPTIONS) {
      aspectOptions.appendChild(el('button', {
        class: 'choice',
        type: 'button',
        'aria-pressed': state.project.aspectRatio === option.value ? 'true' : 'false',
        on: {
          click: () => {
            setIn('project', { aspectRatio: option.value });
            drawAspect();
          },
        },
      }, [
        el('span', { class: 'choice-title', text: option.label }),
        el('span', { class: 'choice-meta', text: option.meta }),
      ]));
    }
  }

  function drawMoods(moods) {
    clear(moodRow);
    if (!moods.length) {
      moodRow.appendChild(el('span', { class: 'hint', text: 'O servidor ainda nao devolveu ambientes musicais.' }));
      return;
    }
    for (const mood of moods) {
      moodRow.appendChild(el('button', {
        class: 'chip',
        type: 'button',
        'aria-pressed': state.music.mood === mood ? 'true' : 'false',
        text: mood,
        on: { click: () => { setIn('music', { mood }); drawMoods(moods); } },
      }));
    }
  }

  // -------------------------------------------------------- catalog loading
  await Promise.allSettled([
    loadLanguages(ctx.signal),
    loadPresets(ctx.signal),
    loadVoices(state.project.language, ctx.signal),
    loadMoods(ctx.signal),
  ]);
  if (ctx.signal.aborted) return panel;

  fillSelect(
    language.select,
    state.catalog.languages.length
      ? state.catalog.languages.map((item) => ({ value: item.code, label: item.label || item.code }))
      : [{ value: state.project.language, label: state.project.language }],
    state.project.language,
  );
  language.select.addEventListener('change', async (event) => {
    setIn('project', { language: event.target.value });
    try {
      await loadVoices(event.target.value, ctx.signal);
      fillVoices();
    } catch (error) {
      if (!error.cancelled) toast(error.message, 'err', 'Vozes');
    }
  });

  function fillVoices() {
    const voices = state.catalog.voices;
    fillSelect(voiceSelect.select, voices.length
      ? voices.map((item) => ({ value: item.id, label: `${item.name} (${item.locale || 'sem locale'})` }))
      : [{ value: '', label: 'Sem vozes para este idioma' }],
    state.voice.id);
    voiceSelect.select.disabled = !voices.length;
    if (voices.length) {
      const preferred = voices.some((item) => item.id === state.voice.id) ? state.voice.id : voices[0].id;
      voiceSelect.select.value = preferred;
      setIn('voice', { id: preferred });
    }
    voiceSelect.select.onchange = (event) => setIn('voice', { id: event.target.value });
  }
  fillVoices();

  const moods = state.catalog.moods.length ? state.catalog.moods : ['ambient'];
  if (!moods.includes(state.music.mood)) setIn('music', { mood: moods[0] });
  drawMoods(moods);
  drawAspect();

  // ------------------------------------------------------- availability
  try {
    available = await api.probeGenerateRoute(ctx.signal);
  } catch {
    available = false;
  }
  if (ctx.signal.aborted) return panel;

  if (available) {
    noticeSlot.appendChild(banner('ok', 'Orquestrador activo',
      'Um pedido cria o guiao, a narracao e o MP4. O progresso aparece ao lado.'));
  } else {
    runButton.disabled = true;
    noticeSlot.append(
      banner('warn', 'Geracao automatica indisponivel neste servidor',
        'POST /api/generate ainda nao esta montado. O fluxo manual escreve o guiao, narra e renderiza com os mesmos campos.'),
      manualButton(),
    );
  }

  runButton.addEventListener('click', () => withBusy(runButton, async () => {
    const text = topic.input.value.trim();
    if (!text) {
      topic.input.setAttribute('aria-invalid', 'true');
      toast('Escreva um tema antes de gerar.', 'warn');
      topic.input.focus();
      return;
    }
    const outcome = await panel.submit(buildPayload({ topic: text, sectionCount: Number(sections.input.value) || 5 }));
    if (outcome === 'completed') runButton.disabled = available ? false : true;
  }));

  if (ctx.params && ctx.params.topic) topic.input.value = ctx.params.topic;

  return panel;
}

/**
 * The one place the briefing becomes a POST /api/generate body. Field names
 * follow GenerationRequest in backend/services/generator.py, which builds the
 * request with **params: an unknown key is rejected, so this map must not
 * drift. Swap it in one function if the route changes its body.
 */
export function buildPayload({ topic, sectionCount }) {
  return {
    topic,
    language: state.project.language,
    voice: state.voice.id,
    tts_provider: state.voice.provider,
    rate: state.voice.rate,
    section_count: sectionCount || 5,
    tone: state.project.tone,
    duration_target: Number(state.project.durationTarget) || 60,
    custom_instructions: state.project.customInstructions || '',
    aspect_ratio: state.project.aspectRatio,
    preset: state.preset,
    subtitle_style: state.style,
    music_track: state.music.track || '',
    music_mood: state.music.mood,
    music_volume: Number(state.music.volume),
    duck_voice: Boolean(state.music.duckVoice),
    include_captions: Boolean(state.project.includeCaptions),
    project_prefix: state.project.name || '',
  };
}

function fillSelect(select, options, selected) {
  while (select.firstChild) select.removeChild(select.firstChild);
  for (const option of options) {
    const node = el('option', { value: String(option.value), text: String(option.label) });
    if (String(option.value) === String(selected)) node.selected = true;
    select.appendChild(node);
  }
}
