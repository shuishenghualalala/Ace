// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BackendConfig, ModelOption } from '../../src/ui/backend-client';
import { hasUserConfiguredModel, startModelTour } from '../../src/ui/features/model-tour';
import { startWikiTour } from '../../src/ui/features/wiki-tour';

function config(profiles: ModelOption[]): BackendConfig {
  return {
    model: profiles[0]?.model || 'default',
    has_key: false,
    base_url: '',
    active_model_id: profiles[0]?.id || 'default',
    models: profiles,
    model_profiles: profiles,
  };
}

describe('first-use model tour eligibility', () => {
  it('shows for an account that only sees built-in models', () => {
    expect(hasUserConfiguredModel(config([
      { id: 'default', name: 'Default', model: 'default', has_key: false, loaded: true, builtin: true },
    ]))).toBe(false);
  });

  it('does not show after the account adds a private model', () => {
    expect(hasUserConfiguredModel(config([
      { id: 'default', name: 'Default', model: 'default', has_key: false, loaded: true, builtin: true },
      { id: 'deepseek', name: 'DeepSeek', model: 'deepseek-chat', has_key: true, loaded: true, builtin: false },
    ]))).toBe(true);
  });

  it('still shows when a private model record has no API key', () => {
    expect(hasUserConfiguredModel(config([
      { id: 'default', name: 'Default', model: 'default', has_key: false, loaded: true, builtin: true },
      { id: 'deepseek', name: 'DeepSeek', model: 'deepseek-chat', has_key: false, loaded: true, builtin: false },
    ]))).toBe(false);
  });
});

describe('model tour flow', () => {
  afterEach(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    document.body.innerHTML = '';
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('opens settings, the model pane, and the add-model form without submitting it', () => {
    document.body.innerHTML = `
      <button id="settings-btn">设置</button>
      <div id="settings-modal">
        <button data-settings-pane="model">模型</button>
        <section id="settings-pane-model" hidden>
          <button id="cfg-model-add">添加模型</button>
        </section>
      </div>
      <div id="model-connect-overlay" hidden><form id="cfg-model-form"><input id="cfg-model-id" /></form></div>
    `;
    const modal = document.getElementById('settings-modal')!;
    const nav = document.querySelector<HTMLElement>('[data-settings-pane="model"]')!;
    const pane = document.getElementById('settings-pane-model')!;
    const overlay = document.getElementById('model-connect-overlay')!;
    document.getElementById('settings-btn')!.addEventListener('click', () => modal.classList.add('show'));
    nav.addEventListener('click', () => {
      nav.classList.add('is-active');
      pane.hidden = false;
    });
    document.getElementById('cfg-model-add')!.addEventListener('click', () => { overlay.hidden = false; });

    document.querySelectorAll<HTMLElement>('#settings-btn, [data-settings-pane="model"], #cfg-model-add, #cfg-model-id')
      .forEach((element) => {
        element.getBoundingClientRect = () => ({
          x: 100,
          y: 100,
          left: 100,
          top: 100,
          right: 220,
          bottom: 140,
          width: 120,
          height: 40,
          toJSON: () => ({}),
        });
      });
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0);
      return 1;
    });

    startModelTour();
    const next = () => document.querySelector<HTMLButtonElement>('[data-tour-next]')!.click();
    expect(document.querySelector('.model-tour')).not.toBeNull();
    expect(document.querySelector('.wiki-tour__tooltip')?.classList).toContain('mw-tour-card');
    expect(document.querySelector('.mw-tour-card__spark')).not.toBeNull();
    expect(document.querySelector('.mw-tour-card__actions')).not.toBeNull();
    expect(document.querySelector('.wiki-tour__btn')).toBeNull();
    expect(document.querySelector<HTMLButtonElement>('[data-tour-prev]')?.hidden).toBe(true);
    next();
    expect(modal.classList.contains('show')).toBe(true);
    const highlight = document.querySelector<HTMLElement>('.wiki-tour__highlight')!;
    expect(highlight.style.getPropertyValue('--mw-runtime-left')).toBe('98px');
    expect(highlight.style.getPropertyValue('--mw-runtime-top')).toBe('98px');
    expect(highlight.style.getPropertyValue('--mw-runtime-width')).toBe('124px');
    expect(highlight.style.getPropertyValue('--mw-runtime-height')).toBe('44px');
    expect(highlight.style.getPropertyValue('--mw-runtime-border-radius')).toBe('14px');
    next();
    expect(nav.classList.contains('is-active')).toBe(true);
    expect(pane.hidden).toBe(false);
    next();
    expect(overlay.hidden).toBe(false);
    expect(document.querySelector('.wiki-tour__title')?.textContent).toBe('填写连接信息并保存');
    next();
    expect(document.querySelector('.model-tour')).toBeNull();
    expect(localStorage.getItem('crew.desktop.modelTourSeen.v1')).toBe('1');
  });
});

describe('shared Spotlight appearance', () => {
  afterEach(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    document.body.innerHTML = '';
  });

  it('uses the external-assistance guide card for Wiki', () => {
    document.body.innerHTML = `
      <section data-wiki-agent-panel>Wiki Agent</section>
      <button id="wiki-kb-select">教程</button>
    `;
    document.querySelectorAll<HTMLElement>('[data-wiki-agent-panel], #wiki-kb-select')
      .forEach((element) => {
        element.getBoundingClientRect = () => ({
          x: 80,
          y: 100,
          left: 80,
          top: 100,
          right: 320,
          bottom: 220,
          width: 240,
          height: 120,
          toJSON: () => ({}),
        });
      });

    startWikiTour();

    expect(document.querySelector('.wiki-tour__tooltip')?.classList).toContain('mw-tour-card');
    expect(document.querySelector('.mw-tour-card__top .wiki-tour__progress')?.textContent).toBe('1 / 2');
    expect(document.querySelector('.mw-tour-card__spark')).not.toBeNull();
    expect(document.querySelector('.mw-tour-card__actions')).not.toBeNull();
    expect(document.querySelector('.wiki-tour__btn')).toBeNull();
    expect(document.querySelector<HTMLButtonElement>('[data-tour-prev]')?.hidden).toBe(true);
  });
});
