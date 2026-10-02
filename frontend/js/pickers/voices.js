/*
 * Dark Studio - pickers/voices.js
 * Voice picker for POST /api/tts. The catalogue from /api/voices is grouped by
 * locale so a pt-PT project only shows pt-PT voices first, the provider list
 * comes from /api/tts/status (providers that are not available are shown but
 * not selectable, because the UI must not offer a capability the server
 * cannot deliver), and rate/pitch map onto the TtsRequest prosody strings.
 */

import { el, clear, selectField, field, rangeField, button, skeletonStack, emptyState, toast, withBusy } from '../ui.js';
import { state, setIn } from '../state.js';
import * as api from '../api.js';
import { groupVoicesByLocale, loadVoices } from '../catalog.js';

const SIGNED = (value, unit) => `${value >= 0 ? '+' : ''}${value}${unit}`;

export function createVoicePicker({ signal, onTest }) {
  const node = el('div', { class: 'stack-4' });
  let status = null;

  const body = el('div', { class: 'stack-4' });
  node.appendChild(body);

  async function refresh() {
    clear(body);
    body.appendChild(skeletonStack(2));
    try {
      const [loaded] = await Promise.all([
        api.getTtsStatus(signal),
        loadVoices(state.project.language, signal),
      ]);
      status = loaded;
      draw();
    } catch (error) {
      if (error.cancelled) return;
      clear(body);
      body.appendChild(emptyState({
        title: 'Vozes indisponiveis',
        text: error.message || 'Nao foi possivel ler o catalogo de vozes.',
        actionLabel: 'Tentar de novo',
        onAction: () => refresh(),
      }));
    }
  }

  function draw() {
    clear(body);
    const providers = (status && status.providers) || {};
    const names = Object.keys(providers);
    const providerOptions = names.length
      ? names.map((name) => ({
          value: name,
          label: providers[name].available
            ? name
            : `${name} (indisponivel${providers[name].requires_key ? ', requer chave' : ''})`,
        }))
      : [{ value: 'edge', label: 'edge' }];

    const provider = selectField({
      label: 'Fornecedor de voz', id: 'voice-provider',
      options: providerOptions,
      value: state.voice.provider,
      onChange: async (event) => {
        setIn('voice', { provider: event.target.value });
        draw();
      },
    });

    const groups = groupVoicesByLocale();
    const voiceSelect = el('select', { class: 'select', id: 'voice-id' });
    if (!groups.length) {
      voiceSelect.appendChild(el('option', { value: '', text: 'Nenhuma voz neste idioma' }));
      voiceSelect.disabled = true;
    } else {
      for (const group of groups) {
        const optgroup = el('optgroup', { attrs: { label: group.locale } });
        for (const item of group.items) {
          optgroup.appendChild(el('option', {
            value: item.id,
            text: `${item.name} - ${item.gender || 'neutro'}`,
          }));
        }
        voiceSelect.appendChild(optgroup);
      }
      const preferred = state.voice.id
        || (status && status.default_voice)
        || (groups[0].items[0] && groups[0].items[0].id);
      const known = state.catalog.voices.some((item) => item.id === preferred);
      voiceSelect.value = known ? preferred : (groups[0].items[0].id || '');
      setIn('voice', { id: voiceSelect.value });
      voiceSelect.addEventListener('change', (event) => setIn('voice', { id: event.target.value }));
    }

    const voice = {
      node: el('div', { class: 'field' }, [
        el('label', { attrs: { for: 'voice-id' }, text: 'Voz' }),
        voiceSelect,
      ]),
      select: voiceSelect,
    };

    const custom = field({
      label: 'Identificador da voz (fornecedor com chave)',
      id: 'voice-custom',
      value: state.voice.id,
      placeholder: 'ex.: alloy, pt-PT-RuiNeural',
      hint: 'Os fornecedores com chave usam os seus proprios identificadores.',
    });
    custom.input.addEventListener('input', (event) => setIn('voice', { id: event.target.value }));

    const rate = rangeField({
      label: 'Velocidade', id: 'voice-rate',
      min: -50, max: 50, step: 5, value: Number(String(state.voice.rate).replace('%', '')) || 0,
      format: (v) => SIGNED(v, '%'),
      onInput: (v) => setIn('voice', { rate: SIGNED(v, '%') }),
    });
    const pitch = rangeField({
      label: 'Tom', id: 'voice-pitch',
      min: -20, max: 20, step: 1, value: Number(String(state.voice.pitch).replace('Hz', '')) || 0,
      format: (v) => SIGNED(v, 'Hz'),
      onInput: (v) => setIn('voice', { pitch: SIGNED(v, 'Hz') }),
    });

    const testButton = button({
      label: 'Testar voz',
      variant: 'ghost',
      size: 'sm',
      iconName: 'mic',
      onClick: () => withBusy(testButton, async () => {
        if (!state.voice.id) {
          toast('Escolha uma voz antes de testar.', 'warn');
          return;
        }
        if (!onTest) return;
        await onTest({
          provider: state.voice.provider,
          voice: state.voice.id,
          rate: state.voice.rate,
          pitch: state.voice.pitch,
        });
      }),
    });

    body.appendChild(el('div', { class: 'grid-2' }, [provider.node, voice.node]));
    body.appendChild(el('div', { class: 'grid-2' }, [rate.node, pitch.node]));
    if (state.voice.provider !== 'edge') body.appendChild(custom.node);
    body.appendChild(el('div', { class: 'row' }, [testButton]));

    if (status && status.error) {
      body.appendChild(el('p', { class: 'hint', text: `Aviso do servidor: ${status.error}` }));
    }
  }

  refresh();
  return {
    node,
    refresh,
    get value() {
      return {
        provider: state.voice.provider,
        voice: state.voice.id,
        rate: state.voice.rate,
        pitch: state.voice.pitch,
      };
    },
  };
}
