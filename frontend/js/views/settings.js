/*
 * Dark Studio - views/settings.js
 * Every key in .env.example, in the shape POST /api/settings accepts: the
 * ApiSettings model in backend/app.py names every one of them `*_api_key`, so
 * `field` carries that exact body key and `id` stays the short name the DOM
 * and presence() use. Sending `id` used to look right and save nothing, because
 * Pydantic drops a key the model does not declare.
 *
 * A key value is never read back into the DOM: the inputs are empty and say
 * only whether the server reports the key as present. Sending an empty string
 * keeps the stored value, because the endpoint falls back to the environment,
 * so filling in one key never wipes the others.
 */

import {
  el, clear, card, badge, button, banner, emptyState, pageHead, field,
  toast, withBusy, skeletonStack, definitionList, statusDot, pluralize,
} from '../ui.js';
import * as api from '../api.js';
import { loadPresets } from '../catalog.js';

const KEYS = [
  { id: 'openrouter', field: 'openrouter_api_key', label: 'OPENROUTER_API_KEY', group: 'Modelo editorial', why: 'Escreve os guioes. Sem ela o servidor usa o modelo local.' },
  { id: 'openrouter_model', field: 'openrouter_model', label: 'OPENROUTER_MODEL', group: 'Modelo editorial', why: 'Tem de terminar em :free.', text: true },
  { id: 'pexels', field: 'pexels_api_key', label: 'PEXELS_API_KEY', group: 'Stock media', why: 'Imagens de fundo para as cenas.' },
  { id: 'pixabay', field: 'pixabay_api_key', label: 'PIXABAY_API_KEY', group: 'Stock media', why: 'Alternativa ao Pexels.' },
  { id: 'gemini', field: 'gemini_api_key', label: 'GEMINI_API_KEY', group: 'Fornecedores alternativos', why: 'Opcional.' },
  { id: 'openai', field: 'openai_api_key', label: 'OPENAI_API_KEY', group: 'Fornecedores alternativos', why: 'Opcional.' },
  { id: 'youtube', field: 'youtube_api_key', label: 'YOUTUBE_API_KEY', group: 'Fornecedores alternativos', why: 'So para analise de metadados.' },
  { id: 'azure_speech_key', field: 'azure_speech_key', label: 'AZURE_SPEECH_KEY', group: 'Sintese de voz', why: 'Com a regiao, a Azure passa a ser preferida.' },
  { id: 'azure_speech_region', field: 'azure_speech_region', label: 'AZURE_SPEECH_REGION', group: 'Sintese de voz', why: 'Ex.: westeurope.', text: true },
];

export async function render(root, ctx) {
  root.appendChild(pageHead(
    'Definicoes',
    'Chaves e verificacoes',
    'As chaves ficam no ficheiro .env do servidor. Esta pagina nunca mostra o valor de uma chave.',
  ));

  const statusBody = el('div', { class: 'stack-2' }, [skeletonStack(3)]);
  const keysBody = el('div', { class: 'stack-4' }, [skeletonStack(2)]);
  const preflightBody = el('div', { class: 'stack-2' }, [skeletonStack(2, true)]);
  const resultBox = el('div', { class: 'stack-2' });

  const saveButton = button({ label: 'Guardar chaves', iconName: 'check' });
  const testButton = button({ label: 'Testar chave de IA', variant: 'ghost', iconName: 'wand' });

  root.append(
    card({ title: 'Estado por fornecedor', body: [statusBody] }),
    card({
      title: 'Chaves da API',
      subtitle: 'Deixar vazio mantem o valor que ja esta guardado.',
      body: [keysBody, el('div', { class: 'row' }, [saveButton, testButton]), resultBox],
    }),
    card({ title: 'Verificacao do sistema', body: [preflightBody] }),
  );

  const inputs = {};

  function presence(providers, tts) {
    const media = (providers && providers.providers) || {};
    const narration = (tts && tts.providers) || {};
    const azure = narration.azure || {};
    return {
      openrouter: Boolean(media.openrouter && media.openrouter.enabled),
      openrouter_model: (media.openrouter && media.openrouter.model) || '',
      pexels: Boolean(media.pexels && media.pexels.enabled),
      pixabay: Boolean(media.pixabay && media.pixabay.enabled),
      gemini: Boolean(media.gemini && media.gemini.enabled),
      openai: Boolean(media.openai && media.openai.enabled),
      youtube: Boolean(media.youtube && media.youtube.enabled),
      azure_speech_key: Boolean(azure.available),
      azure_speech_region: Boolean(azure.available),
    };
  }

  function drawStatus(providers, tts) {
    clear(statusBody);
    const present = presence(providers, tts);
    const rows = [
      ['OpenRouter', present.openrouter, providers && providers.providers
        ? providers.providers.openrouter && providers.providers.openrouter.enabled
          ? `modelo ${providers.providers.openrouter.model}`
          : 'sem chave: guioes com o modelo local'
        : 'estado desconhecido'],
      ['Pexels', present.pexels, present.pexels ? 'chave presente' : 'sem chave'],
      ['Pixabay', present.pixabay, present.pixabay ? 'chave presente' : 'sem chave'],
      ['Gemini', present.gemini, present.gemini ? 'chave presente' : 'sem chave'],
      ['OpenAI', present.openai, present.openai ? 'chave presente' : 'sem chave'],
      ['YouTube', present.youtube, present.youtube ? 'chave presente' : 'sem chave'],
      ['Azure Speech', present.azure_speech_key, present.azure_speech_key
        ? 'disponivel para narracao' : 'indisponivel: a narracao usa edge-tts'],
    ];
    const list = el('div', { class: 'list' });
    for (const [label, ok, detail] of rows) {
      list.appendChild(el('div', { class: 'list-row' }, [
        statusDot(ok ? 'ok' : 'warn'),
        el('span', { class: 'grow' }, [
          el('span', { class: 'list-title', text: label }),
          el('span', { class: 'list-meta', text: detail }),
        ]),
        badge(ok ? 'presente' : 'em falta', ok ? 'ok' : 'warn'),
      ]));
    }
    statusBody.appendChild(list);
    const models = (providers && providers.providers && providers.providers.openrouter) || {};
    statusBody.appendChild(definitionList([
      ['Modelo em uso', models.model || 'desconhecido'],
      ['So modelos :free', models.free_only === false ? 'nao' : 'sim'],
      ['Pedidos gratuitos hoje', models.quota_remaining == null ? '--' : `${models.quota_remaining}/${models.quota_limit == null ? '?' : models.quota_limit}`],
    ]));
  }

  function drawKeys(present) {
    clear(keysBody);
    let group = '';
    for (const item of KEYS) {
      if (item.group !== group) {
        group = item.group;
        keysBody.appendChild(el('p', { class: 'section-title', text: group }));
      }
      const known = present[item.id];
      const isSet = item.text ? Boolean(known) : Boolean(known);
      const input = field({
        label: item.label,
        id: `set-${item.id}`,
        type: item.text ? 'text' : 'password',
        value: item.id === 'openrouter_model' ? String(known || 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free') : '',
        placeholder: isSet ? 'Ja configurada no servidor' : 'Por configurar',
        hint: `${item.why} Estado actual: ${isSet ? 'presente' : 'em falta'}.`,
        attrs: item.text ? {} : { autocomplete: 'new-password' },
      });
      inputs[item.id] = input.input;
      keysBody.appendChild(el('div', { class: 'grid-2' }, [
        input.node,
        el('div', { class: 'field' }, [
          el('span', { class: 'label', text: 'Estado' }),
          badge(isSet ? 'presente' : 'em falta', isSet ? 'ok' : 'warn'),
        ]),
      ]));
    }
  }

  let current = null;

  async function load() {
    const [providersItem, ttsItem] = await Promise.allSettled([
      api.getProviders(ctx.signal),
      api.getTtsStatus(ctx.signal),
    ]);
    if (ctx.signal.aborted) return;
    const providers = providersItem.status === 'fulfilled' ? providersItem.value : null;
    const tts = ttsItem.status === 'fulfilled' ? ttsItem.value : null;
    current = presence(providers, tts);
    if (providers) drawStatus(providers, tts);
    else {
      clear(statusBody);
      statusBody.appendChild(banner('err', 'Estado indisponivel',
        providersItem.reason ? String(providersItem.reason.message || providersItem.reason) : 'Sem resposta.'));
    }
    drawKeys(current);
  }

  saveButton.addEventListener('click', () => withBusy(saveButton, async () => {
    clear(resultBox);
    resultBox.appendChild(skeletonStack(1));
    const payload = {};
    for (const item of KEYS) payload[item.field] = inputs[item.id] ? inputs[item.id].value.trim() : '';
    try {
      const data = await api.saveSettings(payload, ctx.signal);
      clear(resultBox);
      const active = Object.values((data && data.providers) || {}).filter((item) => item && item.enabled).length;
      resultBox.appendChild(banner('ok', 'Configuracao guardada',
        `O servidor escreveu o .env. Fornecedores com chave: ${active}. As chaves em branco mantiveram o valor anterior.`));
      toast('Definicoes guardadas.', 'ok');
      await load();
    } catch (error) {
      if (error.cancelled) return;
      clear(resultBox);
      resultBox.appendChild(banner('err', 'Nao foi possivel guardar',
        error.message || 'O servidor recusou o modelo escolhido.'));
    }
  }));

  testButton.addEventListener('click', () => withBusy(testButton, async () => {
    clear(resultBox);
    resultBox.appendChild(skeletonStack(1));
    try {
      const data = await api.testAiProvider(ctx.signal);
      clear(resultBox);
      resultBox.appendChild(banner(data.status === 'ok' ? 'ok' : 'warn',
        data.status === 'ok' ? 'A chave responde' : 'A chave nao respondeu',
        `${data.model || 'modelo desconhecido'} respondeu: ${data.response || data.error || 'sem texto'}`));
    } catch (error) {
      if (error.cancelled) return;
      clear(resultBox);
      resultBox.appendChild(banner('err', 'O teste falhou', error.message));
    }
  }));

  async function preflight() {
    const results = await Promise.allSettled([
      api.getHealth(ctx.signal),
      api.getTtsStatus(ctx.signal),
      loadPresets(ctx.signal),
      api.probeGenerateRoute(ctx.signal),
    ]);
    if (ctx.signal.aborted) return;
    clear(preflightBody);
    const labels = ['Servidor (/health)', 'Sintese de voz (/api/tts/status)', 'Catalogo de temas (/api/presets)', 'Geracao automatica (POST /api/generate)'];
    const list = el('div', { class: 'list' });
    results.forEach((item, index) => {
      const ok = item.status === 'fulfilled';
      const value = ok ? item.value : null;
      let detail = '';
      if (ok && typeof value === 'object' && value) {
        if (value.providers) {
          detail = Object.entries(value.providers).filter(([, p]) => p && p.available).map(([name]) => name).join(', ') || 'nenhum';
        } else if (value.presets) detail = pluralize(value.presets.length, 'tema');
        else if (value.default_voice) detail = `${pluralize(value.voices, 'voz')} . voz por omissao ${value.default_voice}`;
        else if (typeof value === 'object' && Object.keys(value).length === 0) detail = 'disponivel';
      } else if (ok) {
        detail = 'disponivel';
      } else {
        detail = String((item.reason && item.reason.message) || item.reason);
      }
      list.appendChild(el('div', { class: 'list-row' }, [
        statusDot(ok ? 'ok' : 'err'),
        el('span', { class: 'grow' }, [
          el('span', { class: 'list-title', text: labels[index] }),
          el('span', { class: 'list-meta', text: detail }),
        ]),
      ]));
    });
    preflightBody.appendChild(list);
  }

  await Promise.allSettled([load(), preflight()]);
  return {};
}
