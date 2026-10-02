/*
 * Dark Studio - views/create.js
 * The manual path, in the order the API demands:
 *   1. conteudo  - POST /api/script  (or POST /api/transcribe with your own audio)
 *   2. narracao  - POST /api/tts     (writes the MP3 and the SRT)
 *   3. composicao + render - composition.js, POST /api/build-video
 *
 * Each step reports what the server actually returned, including a script that
 * fell back to the local model, so a degraded run never looks like a good one.
 */

import {
  el, clear, card, badge, button, banner, skeletonStack, pageHead,
  field, textareaField, selectField, toast, withBusy, definitionList,
  fmtDuration, pluralize,
} from '../ui.js';
import * as api from '../api.js';
import { state, setIn, patch } from '../state.js';
import { loadLanguages, TONE_OPTIONS } from '../catalog.js';
import { createVoicePicker } from '../pickers/voices.js';
import { createCompositionPanel } from '../composition.js';

export async function render(root, ctx) {
  root.appendChild(pageHead(
    'Criar',
    'Fluxo manual',
    'Escreva, narre e componha etapa a etapa, com cada resposta do servidor a vista.',
  ));

  const contentCard = card({ title: '1. Conteudo', body: [skeletonStack(2)] });
  const narrationCard = card({ title: '2. Narracao', body: [skeletonStack(2)] });
  const compositionHost = el('div', { class: 'stack-4' });
  root.append(contentCard, narrationCard, compositionHost);

  const panel = createCompositionPanel(ctx, { topicSource: () => state.project.topic });
  compositionHost.appendChild(panel.node);

  const scriptBody = el('div', { class: 'stack-4' });
  const narrationBody = el('div', { class: 'stack-4' });
  const scriptSlot = el('div', {});
  const narrationSlot = el('div', {});
  contentCard.body.replaceChildren(scriptSlot, scriptBody);
  narrationCard.body.replaceChildren(narrationSlot, narrationBody);

  // ----------------------------------------------------------- step 1 form
  const topic = textareaField({
    label: 'Tema', id: 'cr-topic', rows: 2, value: state.project.topic,
    placeholder: 'Ex.: a historia invisivel dos cabos submarinos',
  });
  topic.input.addEventListener('input', (event) => setIn('project', { topic: event.target.value }));

  const niche = field({ label: 'Nicho (opcional)', id: 'cr-niche', value: state.project.niche, placeholder: 'Usado por /api/strategy' });
  niche.input.addEventListener('input', (event) => setIn('project', { niche: event.target.value }));

  const projectName = field({ label: 'Nome do projeto', id: 'cr-project', value: state.project.name });
  projectName.input.addEventListener('input', (event) => setIn('project', { name: event.target.value }));

  const language = selectField({
    label: 'Idioma', id: 'cr-language',
    options: [{ value: state.project.language, label: state.project.language }],
    value: state.project.language,
  });
  const tone = selectField({
    label: 'Tom', id: 'cr-tone', options: TONE_OPTIONS, value: state.project.tone,
  });
  const sections = field({ label: 'Seccoes', id: 'cr-sections', type: 'number', value: String(state.project.sectionCount), attrs: { min: 1, max: 20 } });
  const duration = field({ label: 'Duracao alvo (s)', id: 'cr-duration', type: 'number', value: String(state.project.durationTarget), attrs: { min: 15, max: 900 } });
  const instructions = textareaField({
    label: 'Instrucoes adicionais', id: 'cr-instructions', rows: 2,
    value: state.project.customInstructions,
    placeholder: 'Ex.: termina com uma pergunta ao espectador',
  });
  const scriptButton = button({ label: 'Gerar guiao', iconName: 'spark' });

  const audioInput = el('input', { class: 'file-input', id: 'cr-audio', type: 'file', accept: 'audio/*' });
  const transcribeButton = button({ label: 'Transcrever audio', variant: 'ghost', iconName: 'mic' });

  scriptBody.append(
    el('div', { class: 'grid-2' }, [projectName.node, niche.node]),
    topic.node,
    el('div', { class: 'grid-2' }, [language.node, tone.node]),
    el('div', { class: 'grid-2' }, [sections.node, duration.node]),
    instructions.node,
    el('div', { class: 'row' }, [scriptButton]),
    el('div', { class: 'stack-2' }, [
      el('span', { class: 'section-title', text: 'Ou traga a sua propria narracao' }),
      audioInput,
      el('div', { class: 'row' }, [transcribeButton]),
    ]),
  );

  scriptButton.addEventListener('click', () => withBusy(scriptButton, async () => {
    const text = topic.input.value.trim();
    if (!text) {
      topic.input.setAttribute('aria-invalid', 'true');
      toast('Escreva um tema.', 'warn');
      topic.input.focus();
      return;
    }
    topic.input.removeAttribute('aria-invalid');
    clear(scriptSlot);
    scriptSlot.appendChild(skeletonStack(2));
    try {
      const script = await api.createScript({
        topic: text,
        project_name: state.project.name,
        language: language.select.value,
        section_count: Number(sections.input.value) || 5,
        tone: tone.select.value,
        duration_target: Number(duration.input.value) || 60,
        custom_instructions: instructions.input.value.trim(),
      }, ctx.signal);
      patch({ script, segments: [] });
      renderScript(script);
      setIn('project', { durationTarget: Number(duration.input.value) || 60 });
      toast(script.degraded ? 'Guiao local (modelo indisponivel).' : 'Guiao pronto.', script.degraded ? 'warn' : 'ok');
    } catch (error) {
      if (error.cancelled) return;
      clear(scriptSlot);
      scriptSlot.appendChild(banner('err', 'Nao foi possivel escrever o guiao', error.message));
    }
  }));

  transcribeButton.addEventListener('click', () => withBusy(transcribeButton, async () => {
    const file = audioInput.files && audioInput.files[0];
    if (!file) {
      toast('Escolha um ficheiro de audio.', 'warn');
      return;
    }
    clear(scriptSlot);
    scriptSlot.appendChild(skeletonStack(2));
    try {
      const result = await api.transcribeAudio(file, state.project.name, ctx.signal);
      patch({ segments: Array.isArray(result.segments) ? result.segments : [], script: null });
      clear(scriptSlot);
      scriptSlot.appendChild(card({
        title: 'Transcricao pronta',
        body: [segmentTable(result.segments || []), definitionList([
          ['Ficheiro de audio', result.audio_file],
          ['Legendas', result.srt_file],
          ['Segmentos', (result.segments || []).length],
        ])],
        tight: true,
      }));
      toast('Transcricao concluida.', 'ok');
      drawNarration();
    } catch (error) {
      if (error.cancelled) return;
      clear(scriptSlot);
      scriptSlot.appendChild(banner('err', 'A transcricao falhou', error.message));
    }
  }));

  function renderScript(script) {
    clear(scriptSlot);
    const body = [];
    body.push(el('div', { class: 'row' }, [
      el('div', { class: 'grow' }, [
        el('h3', { text: script.title || 'Sem titulo' }),
        el('p', { class: 'muted small', text: script.hook || '' }),
      ]),
      badge(script.degraded ? 'modelo local' : String(script.source || ''), script.degraded ? 'warn' : 'ok'),
    ]));
    if (script.degraded && script.fallback_error) {
      body.push(banner('warn', 'Gerado sem o modelo remoto',
        `O servidor nao conseguiu usar a IA e escreveu com o modelo local: ${script.fallback_error}`));
    }
    const text = textareaField({
      label: 'Texto a narrar', id: 'cr-full-text', rows: 8,
      value: script.full_text || '',
      hint: 'Edita aqui o que a voz vai ler. O resto do fluxo usa este texto.',
    });
    body.push(text.node);
    const list = el('div', { class: 'list' });
    for (const item of script.sections || []) {
      list.appendChild(el('div', { class: 'list-row' }, [
        el('span', { class: 'mono faint', text: String(item.index).padStart(2, '0') }),
        el('span', { class: 'grow' }, [
          el('span', { class: 'list-title', text: item.text || '' }),
          el('span', { class: 'list-meta', text: `termos visuais: ${(item.visual_terms || []).join(', ') || 'nenhum'}` }),
        ]),
      ]));
    }
    body.push(el('details', {}, [
      el('summary', { class: 'small muted', text: `${pluralize((script.sections || []).length, 'seccao', 'seccoes')} do guiao` }),
      list,
    ]));
    body.push(definitionList([
      ['Idioma', script.language],
      ['Tom', script.tone],
      ['Duracao estimada', fmtDuration(script.estimated_seconds)],
      ['Modelo', script.model || 'local'],
      ['Gravado em', script.stored ? 'sim' : 'nao'],
    ]));
    scriptSlot.appendChild(card({ title: 'Guiao', body, tight: true }));
  }

  // ---------------------------------------------------------- step 2: voice
  const narrateButton = button({ label: 'Narrar projeto', iconName: 'mic' });
  const voicePicker = createVoicePicker({
    signal: ctx.signal,
    onTest: async ({ provider, voice, rate, pitch }) => {
      try {
        const result = await api.synthesize({
          project_name: `${state.project.name || 'teste'}_amostra`,
          text: 'Amostra de voz do Dark Studio. Esta frase serve para conferir o timbre.',
          voice, provider, rate, pitch,
        }, ctx.signal);
        toast(`Amostra gerada: ${pluralize((result.segments || []).length, 'segmento')} em ${fmtDuration(result.estimated_seconds)}.`, 'ok');
      } catch (error) {
        if (!error.cancelled) toast(error.message, 'err', 'Amostra de voz');
      }
    },
  });

  function drawNarration() {
    clear(narrationSlot);
    const hasScript = Boolean(state.script);
    const segments = state.segments;
    narrationSlot.appendChild(banner(
      hasScript || segments.length ? 'ok' : 'warn',
      hasScript || segments.length ? 'Conteudo pronto' : 'Falta conteudo',
      hasScript
        ? 'O texto pode ser editado acima e depois narrado.'
        : segments.length
          ? `${pluralize(segments.length, 'segmento')} transcrito.`
          : 'Gere um guiao ou transcreva um audio para poder narrar.',
    ));
    if (segments.length) {
      narrationSlot.appendChild(card({
        title: 'Segmentos',
        body: [segmentTable(segments)],
        tight: true,
      }));
    }
  }

  narrateButton.addEventListener('click', () => withBusy(narrateButton, async () => {
    const textarea = document.getElementById('cr-full-text');
    const text = textarea ? textarea.value.trim() : '';
    try {
      const result = await api.synthesize({
        project_name: state.project.name,
        text,
        voice: voicePicker.value.voice,
        provider: voicePicker.value.provider,
        rate: voicePicker.value.rate,
        pitch: voicePicker.value.pitch,
      }, ctx.signal);
      patch({ segments: Array.isArray(result.segments) ? result.segments : [] });
      drawNarration();
      toast(`Narracao pronta: ${fmtDuration(result.estimated_seconds)} estimados.`, 'ok');
      toast('Agora pode componer e renderizar.', 'info', 'Passo seguinte');
    } catch (error) {
      if (error.cancelled) return;
      toast(error.message, 'err', 'Narracao');
    }
  }));

  narrationBody.append(voicePicker.node, el('div', { class: 'row' }, [narrateButton]));

  await Promise.allSettled([loadLanguages(ctx.signal), panel.ready]);
  if (ctx.signal.aborted) return { dispose: () => {} };

  if (state.catalog.languages.length) {
    clear(language.select);
    for (const item of state.catalog.languages) {
      const option = el('option', { value: item.code, text: item.label || item.code });
      if (item.code === state.project.language) option.selected = true;
      language.select.appendChild(option);
    }
    language.select.addEventListener('change', (event) => setIn('project', { language: event.target.value }));
  }
  language.select.addEventListener('change', (event) => setIn('project', { language: event.target.value }));

  if (state.script) renderScript(state.script);
  drawNarration();
  if (ctx.params && ctx.params.topic) topic.input.value = ctx.params.topic;
  return {};
}

function segmentTable(segments) {
  const rows = segments.map((item) => el('tr', {}, [
    el('td', { class: 'num', text: String(item.index ?? '') }),
    el('td', { class: 'num', text: `${item.start ?? '--'}s` }),
    el('td', { class: 'num', text: `${item.end ?? '--'}s` }),
    el('td', { text: String(item.text || '') }),
  ]));
  return el('div', { class: 'table-wrap' }, [
    el('table', { class: 'table' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '#' }), el('th', { text: 'Inicio' }), el('th', { text: 'Fim' }), el('th', { text: 'Texto' }),
      ])]),
      el('tbody', {}, rows),
    ]),
  ]);
}
