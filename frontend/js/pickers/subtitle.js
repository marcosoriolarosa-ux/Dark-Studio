/*
 * Dark Studio - pickers/subtitle.js
 * Subtitle style editor. Every control maps one-to-one to a field of the
 * SubtitleStyle dataclass in backend/services/style.py, which is what the
 * `subtitle_style` JSON of POST /api/build-video is parsed into. The preview
 * mirrors to_css(): primary colour, stroke, shadow, background, transform,
 * max width, and the per-position container alignment.
 */

import { el, clear, field, selectField, rangeField, toggleField, fmtPercent } from '../ui.js';
import { state, setIn } from '../state.js';

const ALIGN = { top: 'start', upper: 'center', center: 'center', lower: 'center', bottom: 'end' };
const SAMPLE = 'A legenda que o espectador vai ler';

function hexToRgba(hex, alpha) {
  const clean = String(hex || '#000000').replace('#', '');
  const safe = /^[0-9a-fA-F]{6}$/.test(clean) ? clean : '000000';
  const r = parseInt(safe.slice(0, 2), 16);
  const g = parseInt(safe.slice(2, 4), 16);
  const b = parseInt(safe.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

export function createSubtitleEditor() {
  const node = el('div', { class: 'stack-4' });
  const preview = el('div', { class: 'caption-preview' });
  const caption = el('span', { class: 'cap' });
  preview.appendChild(caption);

  function draw() {
    const style = state.style;
    caption.textContent = SAMPLE;
    caption.style.fontFamily = `"${style.font_family}", sans-serif`;
    // The composition is 1080px wide; scale into the preview box so an 18px and
    // a 160px caption are both legible in a narrow panel.
    caption.style.fontSize = `${Math.max(11, Math.round((Number(style.font_size) / 1080) * 320))}px`;
    caption.style.fontWeight = style.bold ? '800' : '400';
    caption.style.color = style.primary_color;
    caption.style.webkitTextStroke = `${Math.max(0, Number(style.stroke_width) / 4)}px ${style.stroke_color}`;
    caption.style.paintOrder = 'stroke fill';
    caption.style.textShadow = style.shadow ? '0 4px 14px rgba(0,0,0,0.85)' : 'none';
    caption.style.textTransform = style.uppercase ? 'uppercase' : 'none';
    caption.style.maxWidth = `${Number(style.max_width_percent)}%`;
    caption.style.backgroundColor = Number(style.background_opacity) > 0
      ? hexToRgba(style.background_color, Number(style.background_opacity))
      : 'transparent';
    preview.style.setProperty('--cap-align', ALIGN[style.position] || 'end');
  }

  function build() {
    clear(node);
    const style = state.style;
    const fonts = state.catalog.fonts.length ? state.catalog.fonts : [style.font_family];
    const positions = state.catalog.positions.length ? state.catalog.positions : [style.position];
    const modes = state.catalog.modes.length ? state.catalog.modes : [style.mode];

    const size = rangeField({
      label: 'Tamanho do texto', id: 'style-font-size',
      min: 18, max: 160, step: 1, value: style.font_size,
      format: (v) => `${v}px`,
      onInput: (v) => { setIn('style', { font_size: v }); draw(); },
    });
    const stroke = rangeField({
      label: 'Contorno', id: 'style-stroke-width',
      min: 0, max: 20, step: 1, value: style.stroke_width,
      format: (v) => `${v}px`,
      onInput: (v) => { setIn('style', { stroke_width: v }); draw(); },
    });
    const opacity = rangeField({
      label: 'Fundo da legenda', id: 'style-bg-opacity',
      min: 0, max: 1, step: 0.05, value: style.background_opacity,
      format: (v) => fmtPercent(v),
      onInput: (v) => { setIn('style', { background_opacity: v }); draw(); },
    });
    const width = rangeField({
      label: 'Largura maxima', id: 'style-max-width',
      min: 40, max: 100, step: 1, value: style.max_width_percent,
      format: (v) => `${v}%`,
      onInput: (v) => { setIn('style', { max_width_percent: v }); draw(); },
    });

    const primary = field({ label: 'Cor do texto', id: 'style-primary-color', type: 'color', value: style.primary_color });
    const strokeColor = field({ label: 'Cor do contorno', id: 'style-stroke-color', type: 'color', value: style.stroke_color });
    const background = field({ label: 'Cor de fundo', id: 'style-bg-color', type: 'color', value: style.background_color });
    primary.input.addEventListener('input', (event) => { setIn('style', { primary_color: event.target.value }); draw(); });
    strokeColor.input.addEventListener('input', (event) => { setIn('style', { stroke_color: event.target.value }); draw(); });
    background.input.addEventListener('input', (event) => { setIn('style', { background_color: event.target.value }); draw(); });

    const font = selectField({
      label: 'Tipo de letra', id: 'style-font',
      options: fonts.map((name) => ({ value: name, label: name })),
      value: style.font_family,
      onChange: (event) => { setIn('style', { font_family: event.target.value }); draw(); },
    });
    const position = selectField({
      label: 'Posicao', id: 'style-position',
      options: positions.map((name) => ({ value: name, label: name })),
      value: style.position,
      onChange: (event) => { setIn('style', { position: event.target.value }); draw(); },
    });
    const mode = selectField({
      label: 'Modo', id: 'style-mode',
      options: modes.map((name) => ({ value: name, label: name })),
      value: style.mode,
      onChange: (event) => { setIn('style', { mode: event.target.value }); draw(); },
    });

    node.appendChild(preview);
    node.appendChild(el('div', { class: 'grid-2' }, [
      font.node, size.node, position.node, mode.node,
      primary.node, strokeColor.node, stroke.node, width.node, background.node, opacity.node,
    ]));
    node.appendChild(el('div', { class: 'grid-3' }, [
      toggleField({
        label: 'Sombra', id: 'style-shadow', checked: style.shadow,
        onChange: (event) => { setIn('style', { shadow: event.target.checked }); draw(); },
      }).node,
      toggleField({
        label: 'Negrito', id: 'style-bold', checked: style.bold,
        onChange: (event) => { setIn('style', { bold: event.target.checked }); draw(); },
      }).node,
      toggleField({
        label: 'Maiusculas', id: 'style-uppercase', checked: style.uppercase,
        onChange: (event) => { setIn('style', { uppercase: event.target.checked }); draw(); },
      }).node,
    ]));
    draw();
  }

  build();
  return {
    node,
    rebuild: build,
    redraw: draw,
    get value() {
      const style = state.style;
      return {
        font_family: style.font_family,
        font_size: Number(style.font_size),
        primary_color: style.primary_color,
        stroke_color: style.stroke_color,
        stroke_width: Number(style.stroke_width),
        shadow: Boolean(style.shadow),
        background_opacity: Number(style.background_opacity),
        background_color: style.background_color,
        position: style.position,
        uppercase: Boolean(style.uppercase),
        bold: Boolean(style.bold),
        max_width_percent: Number(style.max_width_percent),
        mode: style.mode,
      };
    },
  };
}
