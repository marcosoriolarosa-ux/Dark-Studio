/*
 * Dark Studio - views/projects.js
 * What the server actually holds. /api/projects lists the raw upload files;
 * this folds them into one row per project and probes the render route so a
 * project that was never rendered is labelled as such instead of looking ready.
 */

import {
  el, clear, card, badge, button, banner, emptyState, pageHead, toast,
  skeletonStack, statusDot, pluralize, fmtDuration, withBusy,
} from '../ui.js';
import * as api from '../api.js';
import { loadProjectIndex, projectState } from '../projects.js';
import { state, setIn, patch } from '../state.js';

export async function render(root, ctx) {
  const refresh = button({ label: 'Atualizar', variant: 'ghost', size: 'sm', iconName: 'refresh' });
  root.appendChild(pageHead(
    'Projetos',
    'O que ja existe no servidor',
    'Cada projeto e um conjunto de ficheiros: legenda, audio, guiao guardado e o MP4 quando existe.',
    [refresh],
  ));

  const body = el('div', { class: 'stack-2' }, [skeletonStack(3, true)]);
  const filter = el('input', { class: 'input', id: 'proj-filter', placeholder: 'Filtrar por nome', attrs: { maxlength: '60' } });
  root.append(
    card({ body: [el('div', { class: 'field' }, [el('label', { attrs: { for: 'proj-filter' }, text: 'Filtro' }), filter])] }),
    card({ title: 'Inventario', body: [body] }),
  );

  let rows = [];

  function draw() {
    clear(body);
    const term = filter.value.trim().toLowerCase();
    const visible = term ? rows.filter((row) => row.name.toLowerCase().includes(term)) : rows;
    if (!rows.length) {
      body.appendChild(emptyState({
        title: 'Nenhum ficheiro no servidor',
        text: 'Ainda nao ha legenda, audio ou guiao guardado. Gere o primeiro projeto em "Gerar" ou "Criar".',
        actionLabel: 'Gerar video',
        onAction: () => ctx.navigate('generate'),
      }));
      return;
    }
    if (!visible.length) {
      body.appendChild(emptyState({ title: 'Nenhum projeto com esse nome', text: `Filtro: "${filter.value.trim()}".` }));
      return;
    }
    const table = el('table', { class: 'table' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Projeto' }),
        el('th', { text: 'Estado' }),
        el('th', { text: 'Ficheiros' }),
        el('th', { text: 'Accoes' }),
      ])]),
      el('tbody', {}, visible.map(rowNode)),
    ]);
    body.appendChild(el('div', { class: 'table-wrap' }, [table]));
    body.appendChild(el('p', { class: 'hint', text:
      'Estado "renderizado" significa que o servidor respondeu 200 ao ficheiro MP4 deste projeto.' }));
  }

  function rowNode(row) {
    const info = projectState(row);
    const use = button({
      label: 'Usar', size: 'sm', variant: 'ghost',
      onClick: () => {
        setIn('project', { name: row.name });
        toast(`Projeto "${row.name}" seleccionado.`, 'ok');
        ctx.navigate('studio', { project: row.name });
      },
    });
    const open = row.rendered
      ? el('a', {
          class: 'btn btn-ghost btn-sm', target: '_blank', rel: 'noopener',
          href: api.projectVideoUrl(row.name), text: 'MP4',
        })
      : null;
    const highlights = button({
      label: 'Destaques', size: 'sm', variant: 'ghost',
      onClick: () => withBusy(highlights, async () => {
        try {
          const data = await api.getHighlights(row.name, ctx.signal);
          if (data.status !== 'ok') {
            toast(data.error || 'Este projeto ainda nao tem legenda.', 'warn');
            return;
          }
          toast(`${pluralize(data.highlights_count || 0, 'destaque')} em ${fmtDuration(data.total_seconds)}.`, 'ok');
          patch({ shorts: data });
          ctx.navigate('shorts', { project: row.name });
        } catch (error) {
          if (!error.cancelled) toast(error.message, 'err', 'Destaques');
        }
      }),
    });
    return el('tr', {}, [
      el('td', {}, [
        el('div', { class: 'row' }, [statusDot(info.tone), el('span', { class: 'list-title', text: row.name })]),
        el('div', { class: 'list-meta', text: info.detail }),
      ]),
      el('td', {}, [badge(info.label, info.tone)]),
      el('td', { class: 'small muted', text: row.assets.join(' ') }),
      el('td', {}, [el('div', { class: 'row' }, [use, open, highlights].filter(Boolean))]),
    ]);
  }

  filter.addEventListener('input', draw);

  async function load() {
    try {
      rows = await loadProjectIndex(ctx.signal);
      draw();
    } catch (error) {
      if (error.cancelled) return;
      clear(body);
      body.appendChild(banner('err', 'Nao foi possivel ler os projetos', error.message));
    }
  }

  refresh.addEventListener('click', () => withBusy(refresh, load));
  await load();
  if (state.project.name) {
    filter.placeholder = `Filtrar por nome (projecto actual: ${state.project.name})`;
  }
  return {};
}
