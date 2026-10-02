/*
 * Dark Studio - views/shorts.js
 * The cut-down formats: highlights -> POST /api/shorts, the viral variant ->
 * POST /api/build-viral, and the editorial radar -> POST /api/strategy.
 *
 * build-viral needs the optional `librosa` package and answers 503 without it;
 * that message is surfaced as-is rather than hidden behind a generic failure.
 */

import {
  el, clear, card, badge, button, banner, emptyState, pageHead, field,
  toggleField, selectField, toast, withBusy, skeletonStack, definitionList, fmtDuration,
  pluralize, statusDot,
} from '../ui.js';
import * as api from '../api.js';
import { state, setIn, patch } from '../state.js';

const ASPECTS = [
  { value: 'vertical', label: 'Vertical 9:16' },
  { value: 'square', label: 'Quadrado 1:1' },
  { value: 'landscape', label: 'Horizontal 16:9' },
];

export async function render(root, ctx) {
  root.appendChild(pageHead(
    'Formatos curtos',
    'Shorts, viral e estrategia',
    'Corte os melhores momentos, monte a versao viral e teste o angulo antes de gravar.',
  ));

  const projectName = field({ label: 'Projeto', id: 'sh-project', value: state.project.name });
  projectName.input.addEventListener('input', (event) => setIn('project', { name: event.target.value }));
  if (ctx.params && ctx.params.project) {
    projectName.input.value = ctx.params.project;
    setIn('project', { name: ctx.params.project });
  }

  const shortsAspect = selectField({ label: 'Formato', id: 'sh-aspect', options: ASPECTS, value: 'vertical' });
  const karaoke = toggleField({ label: 'Legendas karaoke', id: 'sh-karaoke', checked: true });
  const maxHighlights = field({ label: 'Destaques', id: 'sh-max', type: 'number', value: '3', attrs: { min: 1, max: 5 } });
  const shortsButton = button({ label: 'Gerar short', iconName: 'film' });
  const shortsOut = el('div', { class: 'stack-2' });
  const highlightsOut = el('div', { class: 'stack-2' });

  const viralAspect = selectField({ label: 'Formato', id: 'sh-viral-aspect', options: ASPECTS, value: 'vertical' });
  const viralNiche = field({ label: 'Nicho', id: 'sh-niche', value: state.project.niche || state.project.topic });
  const viralKaraoke = toggleField({ label: 'Legendas karaoke', id: 'sh-viral-karaoke', checked: true });
  const viralButton = button({ label: 'Gerar versao viral', variant: 'ghost', iconName: 'spark' });
  const viralOut = el('div', { class: 'stack-2' });

  const strategyNiche = field({ label: 'Tema ou nicho', id: 'sh-strategy', value: state.project.niche || state.project.topic });
  const strategyContext = field({ label: 'Contexto', id: 'sh-context', type: 'text', value: '' });
  const strategyButton = button({ label: 'Analisar com IA', variant: 'ghost', iconName: 'wand' });
  const strategyOut = el('div', { class: 'stack-2' });

  root.append(
    card({ body: [projectName.node] }),
    card({
      title: 'Highlights',
      subtitle: 'POST /api/shorts corta os melhores momentos da legenda existente.',
      body: [
        el('div', { class: 'grid-3' }, [maxHighlights.node, shortsAspect.node, karaoke.node]),
        el('div', { class: 'row' }, [shortsButton, button({
          label: 'Ver destaques', variant: 'ghost', size: 'sm',
          onClick: () => withBusy(shortsButton, () => loadHighlights()),
        })]),
        shortsOut,
        highlightsOut,
      ],
    }),
    card({
      title: 'Viral',
      subtitle: 'POST /api/build-viral: beat-sync, loop continuo e metadados de plataforma. Precisa do pacote opcional librosa.',
      body: [
        el('div', { class: 'grid-3' }, [viralNiche.node, viralAspect.node, viralKaraoke.node]),
        el('div', { class: 'row' }, [viralButton]),
        viralOut,
      ],
    }),
    card({
      title: 'Radar editorial',
      subtitle: 'POST /api/strategy devolve angulo, gancho, titulos e capitulos.',
      body: [
        el('div', { class: 'grid-2' }, [strategyNiche.node, strategyContext.node]),
        el('div', { class: 'row' }, [strategyButton]),
        strategyOut,
      ],
    }),
  );

  async function loadHighlights() {
    const name = projectName.input.value.trim();
    if (!name) {
      toast('Escreva o nome do projeto.', 'warn');
      return;
    }
    clear(highlightsOut);
    highlightsOut.appendChild(skeletonStack(2));
    try {
      const data = await api.getHighlights(name, ctx.signal);
      patch({ shorts: data });
      clear(highlightsOut);
      if (data.status !== 'ok') {
        highlightsOut.appendChild(banner('warn', 'Sem destaques', data.error || 'Este projeto ainda nao tem legenda.'));
        return;
      }
      if (!data.highlights_count) {
        highlightsOut.appendChild(emptyState({
          title: 'Nenhum destaque detectado',
          text: 'A legenda existe, mas nenhum momento passa no limiar de pontuacao do detector.',
        }));
        return;
      }
      const list = el('div', { class: 'list' });
      for (const item of data.highlights) {
        list.appendChild(el('div', { class: 'list-row' }, [
          statusDot('ok'),
          el('span', { class: 'grow' }, [
            el('span', { class: 'list-title', text: item.text || '(sem texto)' }),
            el('span', { class: 'list-meta', text: `${item.start}s a ${item.end}s . ${fmtDuration(item.end - item.start)}` }),
          ]),
          item.score == null ? null : badge(String(item.score)),
        ]));
      }
      highlightsOut.append(
        el('p', { class: 'small muted', text:
          `${pluralize(data.highlights_count, 'destaque')} . ${fmtDuration(data.total_seconds)} no total.` }),
        list,
      );
    } catch (error) {
      if (error.cancelled) return;
      clear(highlightsOut);
      highlightsOut.appendChild(banner('err', 'Destaques indisponiveis', error.message));
    }
  }

  shortsButton.addEventListener('click', () => withBusy(shortsButton, async () => {
    const name = projectName.input.value.trim();
    if (!name) {
      toast('Escreva o nome do projeto.', 'warn');
      return;
    }
    clear(shortsOut);
    shortsOut.appendChild(skeletonStack(2));
    try {
      const data = await api.buildShorts({
        project_name: name,
        aspect_ratio: shortsAspect.select.value,
        include_karaoke: Boolean(karaoke.input.checked),
        max_highlights: Number(maxHighlights.input.value) || 3,
      }, ctx.signal);
      patch({ shorts: data });
      clear(shortsOut);
      shortsOut.appendChild(shortResult(data, name, 'short'));
      toast('Short pronto.', 'ok');
    } catch (error) {
      if (error.cancelled) return;
      clear(shortsOut);
      shortsOut.appendChild(banner('err', 'O short nao foi gerado', error.message));
    }
  }));

  viralButton.addEventListener('click', () => withBusy(viralButton, async () => {
    const name = projectName.input.value.trim();
    if (!name) {
      toast('Escreva o nome do projeto.', 'warn');
      return;
    }
    clear(viralOut);
    viralOut.appendChild(skeletonStack(2));
    try {
      const data = await api.buildViral({
        project_name: name,
        aspect_ratio: viralAspect.select.value,
        include_karaoke: Boolean(viralKaraoke.input.checked),
        topic: state.project.topic,
        niche: viralNiche.input.value.trim(),
      }, ctx.signal);
      clear(viralOut);
      viralOut.appendChild(shortResult(data, name, 'viral'));
      toast('Versao viral pronta.', 'ok');
    } catch (error) {
      if (error.cancelled) return;
      clear(viralOut);
      viralOut.appendChild(banner('err', 'A versao viral nao foi gerada', error.message));
    }
  }));

  strategyButton.addEventListener('click', () => withBusy(strategyButton, async () => {
    const niche = strategyNiche.input.value.trim();
    if (!niche) {
      toast('Indique um tema ou nicho.', 'warn');
      return;
    }
    clear(strategyOut);
    strategyOut.appendChild(skeletonStack(2));
    try {
      const data = await api.getStrategy({
        niche,
        transcript: strategyContext.input.value.trim(),
      }, ctx.signal);
      clear(strategyOut);
      const degraded = data.source === 'fallback-local';
      strategyOut.appendChild(card({
        tight: true,
        body: [
          el('div', { class: 'row' }, [
            el('span', { class: 'grow' }, [
              el('span', { class: 'list-title', text: data.angle || '' }),
              el('span', { class: 'list-meta', text: `Gancho: ${data.hook || 'sem gancho'}` }),
            ]),
            badge(data.source || 'desconhecido', degraded ? 'warn' : 'ok'),
          ]),
          degraded && data.fallback_error
            ? banner('warn', 'Analise sem a IA', `O servidor usou o texto de reserva: ${data.fallback_error}`)
            : null,
          definitionList([
            ['Direccao visual', data.visual_direction],
            ['Modelo', data.model],
          ]),
          listBlock('Titulos', data.titles),
          listBlock('Capitulos', data.chapters),
        ],
      }));
    } catch (error) {
      if (error.cancelled) return;
      clear(strategyOut);
      strategyOut.appendChild(banner('err', 'A analise falhou', error.message));
    }
  }));

  if (state.shorts && state.shorts.project_name) {
    loadHighlights();
  }
  return {};
}

function shortResult(data, name, kind) {
  const node = el('div', { class: 'stack-2' });
  node.appendChild(el('div', { class: 'row' }, [
    statusDot(data.status === 'error' ? 'err' : 'ok'),
    el('span', { class: 'grow' }, [
      el('span', { class: 'list-title', text: name }),
      el('span', { class: 'list-meta', text: data.message || data.error || 'Sem mensagem.' }),
    ]),
    badge(data.status || 'ok', data.status === 'error' ? 'err' : 'ok'),
  ]));
  node.appendChild(definitionList([
    ['Duracao', data.video_duration ? fmtDuration(data.video_duration) : '--'],
    ['Resolucao', data.resolution || '--'],
    ['Destaques', data.highlights_count == null ? '--' : String(data.highlights_count)],
    ['Cenas com imagem', data.scenes_with_media == null ? '--' : String(data.scenes_with_media)],
    ['Loop', data.loop_duration ? fmtDuration(data.loop_duration) : null],
    ['Gancho', data.hook_duration ? fmtDuration(data.hook_duration) : null],
    ['Ficheiro', data.output_path],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '')));
  if (data.platform_metadata && typeof data.platform_metadata === 'object') {
    node.appendChild(el('details', {}, [
      el('summary', { class: 'small muted', text: 'Metadados de plataforma' }),
      el('pre', { class: 'code', text: JSON.stringify(data.platform_metadata, null, 2) }),
    ]));
  }
  const video = el('video', { class: 'player', controls: true, playsinline: true, attrs: { preload: 'metadata' } });
  video.src = kind === 'short' ? api.shortsVideoUrl(name) : api.projectVideoUrl(name);
  node.appendChild(video);
  return card({ tight: true, body: [node] });
}

function listBlock(title, items) {
  if (!Array.isArray(items) || !items.length) return null;
  const list = el('ul', { class: 'list' });
  for (const item of items) list.appendChild(el('li', { class: 'small muted', text: String(item) }));
  return el('div', { class: 'stack-2' }, [
    el('span', { class: 'section-title', text: title }), list,
  ]);
}
