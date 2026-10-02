/*
 * Dark Studio - pickers/presets.js
 * Theme presets as a visual picker: label, palette swatches and the caption
 * mode each preset implies. Every card maps to the `preset` form field of
 * POST /api/build-video; choosing one also seeds the subtitle editor with the
 * preset's own look, which is what resolve_preset_and_style() starts from.
 */

import { el, clear, emptyState, badge } from '../ui.js';
import { state, patch, applyPresetStyle } from '../state.js';

export function createPresetPicker({ compact = false, onChange } = {}) {
  const node = el('div', {
    class: compact ? 'choices choices-tight' : 'choices',
    role: 'group',
    'aria-label': 'Tema visual',
  });

  function select(preset) {
    patch({ preset: preset.name });
    applyPresetStyle(preset.subtitle);
    render();
    if (onChange) onChange(preset);
  }

  function render() {
    clear(node);
    const presets = state.catalog.presets;
    if (!presets.length) {
      node.appendChild(emptyState({
        title: 'Catalogo de temas indisponivel',
        text: 'O servidor ainda nao devolveu os temas. Reabra esta pagina para tentar de novo.',
      }));
      return;
    }
    for (const preset of presets) {
      const active = preset.name === state.preset;
      node.appendChild(el('button', {
        class: 'choice',
        type: 'button',
        'aria-pressed': active ? 'true' : 'false',
        on: { click: () => select(preset) },
      }, [
        el('span', { class: 'choice-title', text: preset.label || preset.name }),
        el('span', { class: 'choice-meta', text: preset.effect || '' }),
        el('span', { class: 'swatches' },
          (preset.palette || []).slice(0, 5).map((colour) =>
            el('span', { class: 'swatch', attrs: { style: `background:${colour}` } }))),
        preset.subtitle && preset.subtitle.mode ? badge(preset.subtitle.mode) : null,
      ]));
    }
  }

  render();
  return { node, render, get value() { return state.preset; } };
}
