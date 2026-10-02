/*
 * Dark Studio - ui.js
 * DOM helpers, formatting and feedback (toast / banner / empty / skeleton).
 *
 * Rule enforced here: server data never becomes markup. el() writes text with
 * textContent, so a transcript, a track title or an error detail is always a
 * text node. The only markup built in this file is ICONS, a frozen map of SVG
 * literals written by us, consumed through icon().
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

const ICONS = {
  gauge: 'M12 14a2 2 0 100-4 2 2 0 000 4zM13.4 10.6L17 7M4 20h16M12 4a8 8 0 00-8 8',
  spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18',
  sliders: 'M4 7h10M18 7h2M4 17h4M12 17h8M16 4v6M8 14v6',
  film: 'M3 5h18v14H3zM7 5v14M17 5v14M3 9.7h4M3 14.3h4M17 9.7h4M17 14.3h4',
  layers: 'M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17l9 5 9-5',
  gear: 'M12 15.2a3.2 3.2 0 100-6.4 3.2 3.2 0 000 6.4zM19.4 12a7.6 7.6 0 00-.1-1.2l2-1.5-2-3.4-2.3 1a7.6 7.6 0 00-2-1.2L14.6 3H9.4L9 5.7a7.6 7.6 0 00-2 1.2l-2.3-1-2 3.4 2 1.5a7.6 7.6 0 000 2.4l-2 1.5 2 3.4 2.3-1a7.6 7.6 0 002 1.2l.4 2.7h5.2l.4-2.7a7.6 7.6 0 002-1.2l2.3 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z',
  play: 'M7 4.5l12 7.5-12 7.5z',
  download: 'M12 4v10M8 11l4 4 4-4M4 19h16',
  upload: 'M12 20V9M8 13l4-4 4 4M4 5h16',
  check: 'M4.5 12.5l5 5 10-11',
  close: 'M6 6l12 12M18 6L6 18',
  alert: 'M12 4l9 16H3l9-16zM12 10v4M12 17.2v.1',
  clock: 'M12 21a9 9 0 100-18 9 9 0 000 18zM12 7v5.2l3.2 2',
  search: 'M11 18a7 7 0 100-14 7 7 0 000 14zM16.2 16.2L21 21',
  mic: 'M12 15a3 3 0 003-3V7a3 3 0 00-6 0v5a3 3 0 003 3zM5 11a7 7 0 0014 0M12 18v3M9 21h6',
  music: 'M9 18V6l10-2v12M9 18a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0zM19 16a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0z',
  image: 'M4 5h16v14H4zM4 16l4.5-4.5 3.5 3.5 3-3L20 16M9 9.2v.1',
  wand: 'M5 19l10-10M14 5.5l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9L11.5 8l1.9-.6zM19 13l.4 1.2 1.2.4-1.2.4-.4 1.2-.4-1.2-1.2-.4 1.2-.4z',
  refresh: 'M20 12a8 8 0 11-2.6-5.9M20 4v4h-4',
};

/**
 * Inline SVG icon. The geometry comes from the frozen ICONS map, never from
 * data; an unknown name renders an empty span instead of throwing.
 */
export function icon(name, size = 16) {
  const span = el('span', { class: 'icon', 'aria-hidden': 'true' });
  const d = ICONS[name];
  if (!d) return span;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const segment of d.split(' M')) {
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', segment.startsWith('M') ? segment : 'M' + segment);
    svg.appendChild(path);
  }
  span.appendChild(svg);
  return span;
}

/**
 * Build an element. Supported props:
 *   class, text, html (literals only), attrs {}, data {}, aria, on {click}
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'html') node.innerHTML = value; // literal markup only
    else if (key === 'attrs') {
      for (const [name, item] of Object.entries(value)) {
        if (item !== null && item !== undefined && item !== false) {
          node.setAttribute(name, String(item));
        }
      }
    } else if (key === 'data') {
      for (const [name, item] of Object.entries(value)) {
        if (item !== null && item !== undefined) node.dataset[name] = String(item);
      }
    } else if (key === 'aria') node.setAttribute('aria-label', value);
    else if (key === 'on') {
      for (const [event, fn] of Object.entries(value)) node.addEventListener(event, fn);
    } else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

export function append(parent, children) {
  for (const child of [children].flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export const qs = (id) => document.getElementById(id);
export const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* ------------------------------------------------------------- formatting */

export function fmtDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return '--';
  const total = Math.round(value);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes ? `${minutes}m ${String(rest).padStart(2, '0')}s` : `${rest}s`;
}

export function fmtClock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  return `${String(minutes).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

export function fmtDateTime(iso) {
  if (!iso) return '--';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString('pt-PT', { dateStyle: 'short', timeStyle: 'short' });
}

export function fmtPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return `${Math.round(number <= 1 ? number * 100 : number)}%`;
}

export function pluralize(count, singular, plural) {
  const number = Number(count) || 0;
  return `${number} ${number === 1 ? singular : plural || singular + 's'}`;
}

/* -------------------------------------------------------------- feedback */

let toastArea = null;

export function toast(message, kind = 'info', title = '') {
  if (!toastArea || !toastArea.isConnected) {
    toastArea = qs('ds-toast-area') || el('div', { class: 'toast-area', id: 'ds-toast-area' });
    if (!toastArea.parentNode) document.body.appendChild(toastArea);
  }
  const node = el('div', { class: `toast ${kind}`, role: 'status' }, [
    title ? el('strong', { text: title }) : null,
    el('span', { text: message }),
  ]);
  toastArea.appendChild(node);
  setTimeout(() => node.remove(), kind === 'err' ? 8000 : 4200);
  return node;
}

export function banner(kind, title, text) {
  return el('div', { class: `banner banner-${kind}`, role: kind === 'err' ? 'alert' : 'status' }, [
    el('div', {}, [el('strong', { text: title }), text ? el('span', { text }) : null]),
  ]);
}

export function emptyState({ title, text, actionLabel, onAction }) {
  return el('div', { class: 'empty' }, [
    el('strong', { text: title }),
    text ? el('p', { text: text }) : null,
    actionLabel
      ? el('button', { class: 'btn btn-ghost btn-sm', type: 'button', on: { click: onAction } }, [actionLabel])
      : null,
  ]);
}

export function skeletonStack(count = 3, block = false) {
  return el('div', { class: 'skeleton-stack', 'aria-hidden': 'true' },
    Array.from({ length: count }, () => el('div', { class: block ? 'skeleton-block' : 'skeleton' })));
}

export function badge(text, kind = '') {
  return el('span', { class: `badge ${kind ? 'badge-' + kind : ''}`, text: String(text) });
}

export function statusDot(kind = '', live = false) {
  return el('span', { class: `dot ${kind ? 'dot-' + kind : ''} ${live ? 'dot-live' : ''}` });
}

export function kpi(label, value, kind = '') {
  return el('div', { class: 'kpi' }, [
    el('span', { class: 'kpi-label', text: label }),
    el('b', { class: `kpi-value ${kind}`, text: String(value) }),
  ]);
}

export function card({ title, subtitle, actions, body = [], tight = false }) {
  const section = el('section', { class: `card ${tight ? 'card-tight' : ''}` });
  if (title || actions) {
    section.appendChild(el('div', { class: 'card-head' }, [
      el('div', {}, [
        el('h2', { text: title || '' }),
        subtitle ? el('p', { text: subtitle }) : null,
      ]),
      actions ? el('div', { class: 'row' }, actions) : null,
    ]));
  }
  // The body lives in its own element so a view can swap its contents later
  // (card.body.replaceChildren(...)) without rebuilding the card frame.
  const content = el('div', { class: 'stack-4' }, body);
  section.appendChild(content);
  section.body = content;
  return section;
}

export function pageHead(eyebrow, title, description, actions) {
  return el('header', { class: 'page-head' }, [
    el('div', {}, [
      el('p', { class: 'eyebrow', text: eyebrow }),
      el('h1', { text: title }),
      description ? el('p', { text: description }) : null,
    ]),
    actions ? el('div', { class: 'row' }, actions) : null,
  ]);
}

/* ---------------------------------------------------------------- fields */

export function field({ label, id, value = '', type = 'text', placeholder = '', hint = '', attrs = {} }) {
  const input = el('input', {
    class: 'input',
    id,
    type,
    value,
    placeholder,
    attrs: { autocomplete: 'off', ...attrs },
  });
  return {
    node: el('div', { class: 'field' }, [
      el('label', { attrs: { for: id }, text: label }),
      input,
      hint ? el('span', { class: 'hint', text: hint }) : null,
    ]),
    input,
  };
}

export function textareaField({ label, id, value = '', rows = 4, placeholder = '', hint = '' }) {
  const input = el('textarea', { class: 'textarea', id, rows, placeholder });
  input.value = value;
  return {
    node: el('div', { class: 'field' }, [
      el('label', { attrs: { for: id }, text: label }),
      input,
      hint ? el('span', { class: 'hint', text: hint }) : null,
    ]),
    input,
  };
}

export function selectField({ label, id, options = [], value = '', hint = '', onChange }) {
  const select = el('select', { class: 'select', id, on: onChange ? { change: onChange } : {} });
  for (const option of options) {
    const optionValue = String(option.value ?? option);
    const node = el('option', { text: String(option.label ?? option), value: optionValue });
    if (optionValue === String(value)) node.selected = true;
    select.appendChild(node);
  }
  return {
    node: el('div', { class: 'field' }, [
      el('label', { attrs: { for: id }, text: label }),
      select,
      hint ? el('span', { class: 'hint', text: hint }) : null,
    ]),
    select,
  };
}

export function rangeField({ label, id, min, max, step, value, format, onInput }) {
  const out = el('span', { class: 'range-value', text: format ? format(value) : String(value) });
  const input = el('input', {
    class: 'range',
    id,
    type: 'range',
    min,
    max,
    step,
    value,
    on: onInput
      ? {
          input: (event) => {
            const next = Number(event.target.value);
            out.textContent = format ? format(next) : String(next);
            onInput(next);
          },
        }
      : {},
  });
  return {
    node: el('div', { class: 'field' }, [
      el('div', { class: 'row row-end' }, [el('label', { attrs: { for: id }, text: label }), out]),
      input,
    ]),
    input,
    out,
  };
}

export function toggleField({ label, id, checked = false, hint = '', onChange }) {
  const input = el('input', {
    id,
    type: 'checkbox',
    attrs: { checked: checked ? '' : null },
    on: onChange ? { change: onChange } : {},
  });
  return {
    node: el('div', { class: 'stack-2' }, [
      el('label', { class: 'check' }, [input, el('span', { text: label })]),
      hint ? el('span', { class: 'hint', text: hint }) : null,
    ]),
    input,
  };
}

/* --------------------------------------------------------------- buttons */

export function button({ label, onClick, variant = '', size = '', iconName = '', type = 'button', disabled = false }) {
  const node = el('button', {
    class: `btn ${variant ? 'btn-' + variant : ''} ${size ? 'btn-' + size : ''}`,
    type,
    disabled,
    on: onClick ? { click: onClick } : {},
  });
  if (iconName) node.appendChild(icon(iconName));
  node.appendChild(document.createTextNode(label));
  return node;
}

/**
 * Run an async action with a busy button. The button is always re-enabled,
 * including when the action throws, so a failure never leaves a dead control.
 */
export async function withBusy(node, action) {
  if (node.dataset.busy === '1') return undefined;
  node.dataset.busy = '1';
  node.classList.add('is-busy');
  node.disabled = true;
  try {
    return await action();
  } finally {
    delete node.dataset.busy;
    node.classList.remove('is-busy');
    node.disabled = false;
  }
}

export function definitionList(entries) {
  const list = el('dl', { class: 'kv' });
  for (const [key, value] of entries) {
    if (value === null || value === undefined) continue;
    list.appendChild(el('dt', { text: key }));
    list.appendChild(el('dd', { text: String(value) }));
  }
  return list;
}
