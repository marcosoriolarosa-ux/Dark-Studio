/*
 * Dark Studio - views/dashboard.js
 * Answers two questions before any work starts: can this server do the job,
 * and what happened last time. Every number comes from a live endpoint; a
 * failed probe renders as "indisponivel" with the reason, never as a default
 * that would look like data.
 */

import {
  el, append, clear, card, kpi, badge, statusDot, skeletonStack, emptyState,
  pageHead, button, pluralize,
} from '../ui.js';
import * as api from '../api.js';
import { loadPresets } from '../catalog.js';
import { loadProjectIndex, projectState } from '../projects.js';

/** Unwrap one Promise.allSettled entry: the value, or null plus a reason. */
const value = (item) => (item && item.status === 'fulfilled' ? item.value : null);
const reason = (item) => (item && item.status === 'rejected' ? String(item.reason && item.reason.message || item.reason) : null);

export async function render(root, ctx) {
  root.appendChild(pageHead(
    'Painel',
    'Estado do sistema',
    'O que este servidor consegue fazer agora, e o que ja foi feito.',
    [
      button({ label: 'Gerar video', iconName: 'spark', onClick: () => ctx.navigate('generate') }),
      button({ label: 'Fluxo manual', variant: 'ghost', iconName: 'sliders', onClick: () => ctx.navigate('create') }),
    ],
  ));

  const kpiRow = el('div', { class: 'grid-3' });
  const capsBody = el('div', { class: 'stack-2' }, [skeletonStack(3)]);
  const projectsBody = el('div', { class: 'stack-2' }, [skeletonStack(2, true)]);
  root.append(
    kpiRow,
    card({ title: 'Verificacoes de capacidade', body: [capsBody] }),
    card({
      title: 'Projetos',
      subtitle: 'Cada linha e um projeto existente no servidor.',
      body: [projectsBody],
      actions: [button({ label: 'Ver todos', variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('projects') })],
    }),
  );

  const [healthItem, providersItem, presetsItem, projectsItem, routeItem] =
    await Promise.allSettled([
      api.getHealth(ctx.signal),
      api.getProviders(ctx.signal),
      loadPresets(ctx.signal),
      loadProjectIndex(ctx.signal),
      api.probeGenerateRoute(ctx.signal),
    ]);
  if (ctx.signal.aborted) return {};

  renderKpis(kpiRow, { healthItem, providersItem, presetsItem, projectsItem });
  renderCapabilities(capsBody, { providersItem, presetsItem, routeItem });

  const projects = value(projectsItem);
  const projectCard = projects === null
    ? emptyState({ title: 'Projetos indisponiveis', text: reason(projectsItem) })
    : projects.length
      ? el('div', { class: 'list' }, projects.slice(0, 6).map((row) => projectRow(row, ctx)))
      : emptyState({
          title: 'Ainda nao ha projetos',
          text: 'Gere um guiao ou importe uma narracao para o primeiro projeto aparecer aqui.',
          actionLabel: 'Criar o primeiro',
          onAction: () => ctx.navigate('generate'),
        });
  replace(projectsBody, projectCard);
}

function replace(node, child) {
  clear(node);
  append(node, child);
}

function projectRow(row, ctx) {
  const info = projectState(row);
  return el('button', {
    class: 'list-row',
    type: 'button',
    on: { click: () => ctx.navigate('studio', { project: row.name }) },
  }, [
    statusDot(info.tone),
    el('span', { class: 'grow' }, [
      el('span', { class: 'list-title', text: row.name }),
      el('span', { class: 'list-meta', text: `${pluralize(row.assets.length, 'ficheiro')} . ${info.detail}` }),
    ]),
    badge(info.label, info.tone),
  ]);
}

function renderKpis(kpiRow, { healthItem, providersItem, presetsItem, projectsItem }) {
  replace(kpiRow, null);
  const providers = value(providersItem);
  const media = (providers && providers.providers) || {};
  const openrouter = media.openrouter || {};
  const tts = (providers && providers.tts) || {};
  const presets = value(presetsItem);
  const projects = value(projectsItem);
  const quota = typeof openrouter.quota_remaining === 'number'
    ? `${openrouter.quota_remaining}/${openrouter.quota_limit == null ? '?' : openrouter.quota_limit}`
    : '--';

  const entries = [
    ['Servidor', value(healthItem) ? 'online' : 'indisponivel', value(healthItem) ? 'ok' : 'err'],
    ['Modelo IA', openrouter.enabled ? (openrouter.model || 'ativo') : 'sem chave', openrouter.enabled ? '' : 'warn'],
    ['Cota gratuita', quota, openrouter.quota_exhausted ? 'warn' : ''],
    ['Vozes', tts.voices == null ? '--' : String(tts.voices), ''],
    ['Temas', presets ? String(presets.presets.length) : '--', ''],
    ['Projetos', projects ? String(projects.length) : '--', ''],
  ];
  for (const [label, shown, kind] of entries) kpiRow.appendChild(kpi(label, shown, kind));
}

function renderCapabilities(body, { providersItem, presetsItem, routeItem }) {
  replace(body, null);
  const checks = [];
  const providers = value(providersItem);

  if (!providers) {
    checks.push(['Fornecedores', false, reason(providersItem) || 'Endpoint /api/providers indisponivel.']);
  } else {
    const media = providers.providers || {};
    const openrouter = media.openrouter || {};
    checks.push(['IA (OpenRouter)', Boolean(openrouter.enabled), openrouter.enabled
      ? `${openrouter.model} . ${openrouter.quota_remaining == null ? '?' : openrouter.quota_remaining}/${openrouter.quota_limit == null ? '?' : openrouter.quota_limit} pedidos gratuitos restantes`
      : 'Sem OPENROUTER_API_KEY: os guioes caem no modelo local.']);
    checks.push(['Imagens (Pexels)', Boolean(media.pexels && media.pexels.enabled),
      media.pexels && media.pexels.enabled ? 'Chave configurada.' : 'Sem chave: o motor usa material local.']);
    checks.push(['Imagens (Pixabay)', Boolean(media.pixabay && media.pixabay.enabled),
      media.pixabay && media.pixabay.enabled ? 'Chave configurada.' : 'Sem chave.']);
    const narration = Object.entries(providers.tts && providers.tts.providers || {})
      .filter(([, item]) => item && item.available)
      .map(([name]) => name);
    checks.push(['Narracao', narration.length > 0, narration.length
      ? `Disponivel: ${narration.join(', ')}.`
      : 'Nenhum fornecedor de voz disponivel.']);
  }

  const route = value(routeItem);
  checks.push(['Geracao automatica', route === true, route === true
    ? 'POST /api/generate responde: um clique produz o video completo.'
    : 'POST /api/generate ainda nao existe neste servidor: use o fluxo manual em "Criar".']);

  checks.push(['Motor de video (ffmpeg)', null,
    'A API nao expoe o estado do ffmpeg. Se faltar, o render falha com a mensagem do servidor.']);

  const presets = value(presetsItem);
  checks.push(['Catalogo de temas', Boolean(presets && presets.presets.length), presets
    ? `${presets.presets.length} temas, ${presets.fonts.length} tipos de letra.`
    : 'Catalogo indisponivel.']);

  const list = el('div', { class: 'list' });
  for (const [label, ok, detail] of checks) {
    list.appendChild(el('div', { class: 'list-row' }, [
      statusDot(ok === null ? '' : ok ? 'ok' : 'warn'),
      el('span', { class: 'grow' }, [
        el('span', { class: 'list-title', text: label }),
        el('span', { class: 'list-meta', text: detail }),
      ]),
      ok === null ? badge('desconhecido') : badge(ok ? 'ok' : 'em falta', ok ? 'ok' : 'warn'),
    ]));
  }
  body.appendChild(list);
}
