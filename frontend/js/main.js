/*
 * Dark Studio - main.js
 * Router and bootstrap. Owns the navigation, the view lifecycle (mount, abort
 * in-flight requests, dispose timers) and the global error reporting. Views
 * never talk to each other; they receive a context and a signal.
 */

import { el, qs, clear, icon, toast, banner, pageHead, button } from './ui.js';
import * as api from './api.js';
import { patch } from './state.js';
import { loadPresets, loadLanguages } from './catalog.js';

import * as dashboard from './views/dashboard.js';
import * as generate from './views/generate.js';
import * as create from './views/create.js';
import * as studio from './views/studio.js';
import * as projects from './views/projects.js';
import * as shorts from './views/shorts.js';
import * as settings from './views/settings.js';

const ROUTES = [
  { id: 'dashboard', label: 'Painel', icon: 'gauge', render: dashboard.render },
  { id: 'generate', label: 'Gerar', icon: 'spark', render: generate.render },
  { id: 'create', label: 'Criar', icon: 'sliders', render: create.render },
  { id: 'studio', label: 'Estudio', icon: 'film', render: studio.render },
  { id: 'projects', label: 'Projetos', icon: 'layers', render: projects.render },
  { id: 'shorts', label: 'Formatos curtos', icon: 'music', render: shorts.render },
  { id: 'settings', label: 'Definicoes', icon: 'gear', render: settings.render },
];

const DEFAULT_ROUTE = 'dashboard';
let current = null; // { dispose }
let mainSignal = null;

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [id, query] = raw.split('?');
  const route = ROUTES.find((item) => item.id === id) ? id : DEFAULT_ROUTE;
  const params = {};
  if (query) {
    for (const pair of query.split('&')) {
      if (!pair) continue;
      const [key, value = ''] = pair.split('=');
      if (key) params[decodeURIComponent(key)] = decodeURIComponent(value);
    }
  }
  return { route, params };
}

export function navigate(id, params = {}) {
  const query = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
    .join('&');
  const next = `#/${id}${query ? `?${query}` : ''}`;
  if (window.location.hash === next) render();
  else window.location.hash = next;
}

function buildNav() {
  const nav = qs('ds-nav');
  clear(nav);
  ROUTES.forEach((route, index) => {
    const item = el('button', {
      class: 'nav-item',
      type: 'button',
      data: { route: route.id },
      title: `${route.label} (Alt+${index + 1})`,
      on: { click: () => navigate(route.id) },
    }, [icon(route.icon), el('span', { text: route.label })]);
    nav.appendChild(item);
  });
}

function markNav(routeId) {
  for (const item of qs('ds-nav').children) {
    if (item.dataset.route === routeId) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
}

function errorScreen(root, error) {
  root.replaceChildren();
  root.append(
    pageHead('Erro', 'Esta vista nao carregou', 'O servidor ou a rede falharam. Nenhum estado foi alterado.'),
    banner('err', 'Detalhe', error && error.message ? error.message : String(error)),
    button({
      label: 'Tentar de novo',
      iconName: 'refresh',
      onClick: () => render(true),
    }),
    button({ label: 'Voltar ao painel', variant: 'ghost', onClick: () => navigate(DEFAULT_ROUTE) }),
  );
}

async function render(force = false) {
  const { route, params } = parseHash();
  const key = window.location.hash || `#/${DEFAULT_ROUTE}`;
  // The hash is the signature, not just the route: navigating from /studio to
  // /studio?project=other must remount, or the new project would be ignored.
  if (!force && current && current.key === key) return;

  api.abortAll();
  if (current && typeof current.dispose === 'function') {
    try { current.dispose(); } catch (error) { console.error(error); }
  }
  current = { route, key };

  const host = qs('ds-view');
  clear(host);
  markNav(route);
  patch({ route }, { save: false });
  qs('ds-crumb').textContent = ROUTES.find((item) => item.id === route).label;

  const definition = ROUTES.find((item) => item.id === route);
  mainSignal = api.createSignal();
  const ctx = { signal: mainSignal.signal, navigate, params, route };

  try {
    const result = await definition.render(host, ctx);
    if (result && typeof result.dispose === 'function') current = { route, key, dispose: result.dispose };
    if (!mainSignal.signal.aborted) {
      const heading = host.querySelector('h1');
      if (heading) {
        heading.setAttribute('tabindex', '-1');
        heading.focus({ preventScroll: true });
      }
      host.scrollIntoView({ block: 'start' });
    }
  } catch (error) {
    if (mainSignal.signal.aborted || (error && error.cancelled)) return;
    console.error(error);
    errorScreen(host, error);
  }
}

async function boot() {
  buildNav();
  mountStatusChip();
  api.onAuthRedirect((code) => {
    toast('O servidor precisa de uma chave valida para esta operacao.', 'warn', String(code));
    navigate('settings');
  });

  window.addEventListener('hashchange', () => render());
  window.addEventListener('keydown', (event) => {
    if (!event.altKey || event.ctrlKey || event.metaKey) return;
    const index = Number(event.key) - 1;
    if (Number.isInteger(index) && index >= 0 && index < ROUTES.length) {
      event.preventDefault();
      navigate(ROUTES[index].id);
    }
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    if (reason && reason.cancelled) return;
    console.error('Promessa rejeitada sem tratamento:', reason);
    toast(reason && reason.message ? reason.message : 'Ocorreu um erro inesperado.', 'err');
  });

  // Warm the shared catalogue so the first paint of the pickers is instant.
  await Promise.allSettled([
    loadPresets().catch(() => {}),
    loadLanguages().catch(() => {}),
  ]);
  await render(true);
  refreshHealth();
}

const healthChip = {
  node: null,
  set(text, kind, title) {
    if (!this.node) return;
    this.node.textContent = text;
    this.node.className = `chip static ${kind === 'ok' ? 'badge-ok' : kind === 'err' ? 'badge-err' : ''}`;
    if (title) this.node.title = title;
  },
};

async function refreshHealth() {
  try {
    const data = await api.getHealth();
    healthChip.set('Servidor online', 'ok', data.message || '');
  } catch (error) {
    healthChip.set('Servidor indisponivel', 'err', error.message || '');
  }
}

function mountStatusChip() {
  healthChip.node = el('span', { class: 'chip static', id: 'ds-health', text: 'A verificar servidor...' });
  qs('ds-status').appendChild(healthChip.node);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}
