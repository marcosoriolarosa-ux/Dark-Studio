/*
 * Dark Studio - generator.js
 * The one-click pipeline controller: POST /api/generate, then poll
 * GET /api/jobs/{job_id} until the job completes or fails.
 *
 * This is deliberately the only place that knows about the job contract, so
 * the orchestration can move from the UI to a server-side route without
 * touching the form: the view hands over a payload and renders what this
 * controller reports back.
 */

import {
  el, clear, card, badge, button, banner, emptyState, skeletonStack,
  toast, fmtClock, statusDot, definitionList,
} from './ui.js';
import * as api from './api.js';
import { patch } from './state.js';

const STAGES = [
  ['script', 'Escrever o guiao'],
  ['tts', 'Narrar com voz sintetica'],
  ['media', 'Procurar imagens'],
  ['render', 'Compor e renderizar'],
  ['done', 'Publicar o MP4'],
];

const STATUS_LABEL = {
  queued: 'Em fila',
  cancelled: 'Cancelada',
  running: 'A trabalhar',
  completed: 'Concluido',
  failed: 'Falhou',
};

function stageKeyOf(job) {
  const stage = String((job && job.stage) || '').toLowerCase();
  if (!job) return 'idle';
  if (job.status === 'completed') return 'done';
  if (job.status === 'queued') return 'idle';
  if (job.status === 'running') {
    // Running jobs map to their actual pipeline stage
    if (stage.includes('script') || stage.includes('guiao') || stage.includes('guión')) return 'script';
    if (stage.includes('tts') || stage.includes('voice') || stage.includes('voz') || stage.includes('audio')) return 'tts';
    if (stage.includes('media') || stage.includes('image') || stage.includes('asset')) return 'media';
    if (stage.includes('render') || stage.includes('compose') || stage.includes('compos')) return 'render';
    return 'script';
  }
  if (stage.includes('script') || stage.includes('guiao') || stage.includes('guión')) return 'script';
  if (stage.includes('tts') || stage.includes('voice') || stage.includes('voz') || stage.includes('audio')) return 'tts';
  if (stage.includes('media') || stage.includes('image') || stage.includes('asset')) return 'media';
  if (stage.includes('render') || stage.includes('compose') || stage.includes('compos')) return 'render';
  return 'script';
}

export function createGeneratorPanel(ctx, { onUnavailable } = {}) {
  const stagesNode = el('div', { class: 'stages' });
  const progressBar = el('div', { class: 'progress-bar' });
  const progressWrap = el('div', {
    class: 'progress',
    attrs: { role: 'progressbar', 'aria-label': 'Progresso da geracao', 'aria-valuemin': '0', 'aria-valuemax': '100' },
  }, [progressBar]);
  const statusLine = el('p', { class: 'muted small' });
  const elapsedNode = el('span', { class: 'mono faint', text: '00:00' });
  const resultBox = el('div', { class: 'stack' });

  const node = card({
    title: 'Estado da geracao',
    subtitle: 'Cada etapa fica visivel enquanto o servidor trabalha.',
    body: [
      el('div', { class: 'stack-2' }, [
        el('div', { class: 'row row-end' }, [statusLine, elapsedNode]),
        progressWrap,
        stagesNode,
      ]),
      resultBox,
    ],
  });

  let poll = null;
  let clock = null;
  let startedAt = 0;

  function drawStages(activeKey, status) {
    clear(stagesNode);
    const order = STAGES.map(([key]) => key);
    const activeIndex = order.indexOf(activeKey);
    STAGES.forEach(([key, label], index) => {
      let name = 'idle';
      if (activeIndex >= 0 && index < activeIndex) name = 'done';
      else if (activeIndex >= 0 && index === activeIndex) name = 'active';
      stagesNode.appendChild(el('div', { class: 'stage', data: { state: name } }, [
        el('span', { class: 'stage-index', text: String(index + 1).padStart(2, '0') }),
        el('span', { text: label }),
        el('span', {
          class: 'stage-time',
          text: name === 'done' ? 'ok' : (name === 'active' && status === 'queued' ? 'fila' : name === 'active' && status === 'running' ? 'a correr' : ''),
        }),
      ]));
    });
  }

  function setProgress(fraction) {
    const pct = Math.max(0, Math.min(100, Math.round((Number(fraction) || 0) * 100)));
    if (pct === 0) progressWrap.classList.add('progress-indeterminate');
    else progressWrap.classList.remove('progress-indeterminate');
    progressBar.style.width = `${pct}%`;
    progressWrap.setAttribute('aria-valuenow', String(pct));
  }

  function startTimers() {
    stopTimers();
    startedAt = Date.now();
    clock = setInterval(() => {
      elapsedNode.textContent = fmtClock((Date.now() - startedAt) / 1000);
    }, 1000);
  }

  function stopTimers() {
    if (poll) { clearTimeout(poll); poll = null; }
    if (clock) { clearInterval(clock); clock = null; }
  }

  function idle() {
    clear(resultBox);
    resultBox.appendChild(emptyState({
      title: 'Sem geracao em curso',
      text: 'Escreva um tema e prima o botao. O progresso aparece aqui passo a passo.',
    }));
  }

  function projectNameOf(job) {
    const result = (job && job.result) || {};
    const render = result.render_real || result.render || {};
    return result.project_name || render.project_name || result.output_stem || job.project_name || '';
  }

  function renderCompleted(job) {
    clear(resultBox);
    const result = job.result || {};
    const name = projectNameOf(job);
    const render = result.render_real || result.render || {};
    const rendered = render.status === 'rendered'
      || result.status === 'rendered'
      || Boolean(result.output_path || result.video_url);

    const summary = el('div', { class: 'stack-2' }, [
      el('div', { class: 'row' }, [
        statusDot('ok'),
        el('span', { class: 'grow' }, [
          el('span', { class: 'list-title', text: name || 'Projeto concluido' }),
          el('span', { class: 'list-meta', text: job.message || 'O servidor terminou o trabalho.' }),
        ]),
        badge('concluido', 'ok'),
      ]),
    ]);

    if (name && rendered) {
      const video = el('video', { class: 'player', controls: true, playsinline: true, attrs: { preload: 'metadata' } });
      video.src = api.projectVideoUrl(name);
      summary.appendChild(video);
      summary.appendChild(el('div', { class: 'row' }, [
        el('a', {
          class: 'btn btn-ghost btn-sm', target: '_blank', rel: 'noopener',
          href: api.projectVideoUrl(name), text: 'Abrir MP4',
        }),
        button({
          label: 'Abrir no estudio', variant: 'ghost', size: 'sm', iconName: 'film',
          onClick: () => ctx.navigate('studio', { project: name }),
        }),
      ]));
    } else if (name) {
      summary.appendChild(banner('warn', 'Concluido sem MP4 confirmado',
        `O servidor respondeu sem um caminho de video confirmado. Verifique o projeto "${name}".`));
    }

    const facts = [
      ['Identificador', job.job_id],
      ['Etapa final', job.stage],
      ['Criado', job.created_at],
      ['Actualizado', job.updated_at],
      ['Projeto', name || null],
      ['Duracao', typeof render.video_duration === 'number' ? `${render.video_duration}s` : null],
      ['Resolucao', render.resolution || result.resolution || null],
    ].filter(([, item]) => item !== undefined && item !== null && item !== '');

    if (facts.length) summary.appendChild(definitionList(facts));
    resultBox.appendChild(summary);

    if (job.result) {
      resultBox.appendChild(el('details', {}, [
        el('summary', { class: 'small muted', text: 'Resposta completa do servidor' }),
        el('pre', { class: 'code', text: JSON.stringify(job.result, null, 2) }),
      ]));
    }
  }

  async function pollJob(jobId) {
    let job;
    try {
      job = await api.getGenerateJob(jobId, ctx.signal);
    } catch (error) {
      if (error.cancelled || ctx.signal.aborted) return;
      stopTimers();
      statusLine.textContent = 'Perdeu-se o contacto com o servidor.';
      clear(resultBox);
      resultBox.appendChild(banner('err', 'Estado desconhecido',
        `${error.message} O trabalho pode continuar no servidor; volte a ver os projetos daqui a pouco.`));
      return;
    }
    if (ctx.signal.aborted) return;

    patch({ generate: job });
    statusLine.textContent = job.message || STATUS_LABEL[job.status] || job.status || '';
    drawStages(stageKeyOf(job), job.status);

    if (job.status === 'completed') {
      setProgress(1);
      stopTimers();
      renderCompleted(job);
      toast('Video pronto.', 'ok', 'Geracao');
      return 'completed';
    }
    if (job.status === 'cancelled') {
      setProgress(0);
      stopTimers();
      statusLine.textContent = job.message || 'A tarefa foi cancelada.';
      clear(resultBox);
      resultBox.appendChild(banner('warn', 'Geracao cancelada', job.message || 'O servidor cancelou a tarefa.'));
      return 'failed';
    }
    if (job.status === 'failed') {
      setProgress(0);
      stopTimers();
      drawStages('render', 'failed');
      statusLine.textContent = job.error || 'O servidor nao conseguiu terminar.';
      clear(resultBox);
      resultBox.appendChild(banner('err', 'Geracao falhou', job.error || 'Sem detalhe do servidor.'));
      toast(job.error || 'A geracao falhou.', 'err', 'Geracao');
      return 'failed';
    }

    setProgress(job.progress);
    elapsedNode.textContent = fmtClock((Date.now() - startedAt) / 1000);
    poll = setTimeout(() => pollJob(jobId), 1500);
    return 'running';
  }

  /**
   * Submit a payload and follow the job to the end. Resolves to
   * 'completed' | 'failed' | 'running' | 'unavailable' | 'error'.
   */
  async function submit(payload) {
    clear(resultBox);
    resultBox.appendChild(skeletonStack(2));
    startTimers();
    statusLine.textContent = 'A enviar o pedido...';
    setProgress(0);
    drawStages('script');

    let accepted;
    try {
      accepted = await api.startGenerate(payload, ctx.signal);
    } catch (error) {
      stopTimers();
      if (error.cancelled) return 'error';
      if (error.status === 404) {
        clear(resultBox);
        resultBox.appendChild(banner('warn', 'Geracao automatica indisponivel',
          'Este servidor ainda nao tem POST /api/generate. Continue no fluxo manual: escreve o guiao, narra e renderiza com os mesmos campos.'));
        if (onUnavailable) onUnavailable(error);
        return 'unavailable';
      }
      clear(resultBox);
      resultBox.appendChild(banner('err', 'Nao foi possivel iniciar', error.message));
      statusLine.textContent = 'Erro ao iniciar.';
      return 'error';
    }

    patch({ generate: accepted });
    if (!accepted.job_id) {
      stopTimers();
      clear(resultBox);
      resultBox.appendChild(banner('err', 'Resposta incompleta',
        'O servidor aceitou o pedido mas nao devolveu um identificador de tarefa.'));
      return 'error';
    }
    statusLine.textContent = accepted.message || STATUS_LABEL[accepted.status] || 'Pedido aceite.';
    toast(`Tarefa ${accepted.job_id} criada.`, 'ok');
    return pollJob(accepted.job_id);
  }

  drawStages('idle', 'idle');
  idle();

  return { node, submit, dispose: stopTimers, setIdle: idle };
}
