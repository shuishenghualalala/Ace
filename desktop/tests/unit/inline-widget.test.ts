// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { suppressUnsupportedIframeSchemeErrors } from './helpers/happy-dom';
import { bindInlineWidgets, hydrateInlineWidgets, inlineDocument, renderInlineWidget, type InlineWidget } from '../../src/ui/features/inline-widget';
import { inlineSurfaces } from '../../src/ui/features/inline-surfaces';
import { state } from '../../src/ui/state';
import type { ChatMessage } from '../../src/ui/chat-render';

suppressUnsupportedIframeSchemeErrors();

const widget: InlineWidget = { key: 'inline:s:t', sessionId: 's', widgetId: 'widget_123456789abc',
  title: '收益', html: '<input id="cost" type="number">', resourceRevision: 1 };
const snapshot = { parameters: { cost: 7 }, controls: { cost: '7' } };
const send = vi.fn(async () => true);
function mount() {
  const root = renderInlineWidget(widget); document.body.append(root); hydrateInlineWidgets(document.body);
  return { root, frame: root.querySelector('iframe')!, button: root.querySelector('button')! };
}
function emit(frame: HTMLIFrameElement, data: unknown, source = frame.contentWindow) {
  window.dispatchEvent(new MessageEvent('message', { source, data }));
}

beforeEach(() => {
  document.body.replaceChildren(); localStorage.clear(); send.mockClear();
  state.isLoggedIn = true; state.userInfo = { uid: 'owner-a' }; state.activeSessionId = 's';
  bindInlineWidgets(send);
});

describe('desktop inline widgets', () => {
  it('parses each Widget result once while extracting surfaces', () => {
    const message: ChatMessage = { id: 'm', role: 'assistant', timestamp: 1, content: '', toolCalls: ['a', 'b'].map(toolCallId => ({
      name: 'Widget', toolCallId, status: 'done', startedAt: 0, result: JSON.stringify({ ok: true, surface: {
        ...widget, display: 'inline', version: 1, mode: 'widget', status: 'ready',
      } }),
    })) };
    const parse = vi.spyOn(JSON, 'parse');
    expect(inlineSurfaces([message], 's').widgets).toHaveLength(2);
    expect(parse).toHaveBeenCalledTimes(2);
    parse.mockRestore();
  });

  it('uses only successful Widget snapshots belonging to the rendered session and keeps multiple components', () => {
    const message: ChatMessage = { id: 'm', role: 'assistant', timestamp: 1, content: '', toolCalls: ['a', 'b'].map(toolCallId => ({
      name: 'Widget', toolCallId, status: 'done', startedAt: 0, result: JSON.stringify({ ok: true, surface: {
        ...widget, display: 'inline', version: 1, mode: 'widget', status: 'ready',
      } }),
    })) };
    expect(inlineSurfaces([message], 's').widgets).toHaveLength(2);
    expect(inlineSurfaces([message], 'other').widgets).toHaveLength(0);
    message.toolCalls![0].name = 'terminal';
    expect(inlineSurfaces([message], 's').widgets).toHaveLength(1);
  });

  it('isolates the frame, escapes saved state and keeps initialization idempotent', () => {
    const { frame } = mount();
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame.src).toContain('?inline_shell=1');
    const post = vi.spyOn(frame.contentWindow!, 'postMessage');
    emit(frame, { type: 'ace-inline-shell-ready' });
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: 'ace-inline-init' }), '*');
    const initial = frame.src;
    hydrateInlineWidgets(document.body);
    expect(frame.src).toBe(initial);
    expect(inlineDocument('', { parameters: { text: '</script><script>bad()' }, controls: {} }))
      .not.toContain('</script><script>bad()');
  });

  it('ignores forged events, restores saved parameters, and isolates accounts', () => {
    const { frame, root } = mount();
    emit(frame, { type: 'ace-inline-state', value: snapshot }, window);
    expect(localStorage.length).toBe(0);
    emit(frame, { type: 'ace-inline-state', value: snapshot });
    expect(localStorage.length).toBe(1);
    root.remove();
    const restored = mount();
    const post = vi.spyOn(restored.frame.contentWindow!, 'postMessage');
    emit(restored.frame, { type: 'ace-inline-shell-ready' });
    expect(post.mock.calls[0][0].html).toContain('"cost":7');
    restored.root.remove(); state.userInfo = { uid: 'owner-b' };
    const other = mount(); const otherPost = vi.spyOn(other.frame.contentWindow!, 'postMessage');
    emit(other.frame, { type: 'ace-inline-shell-ready' });
    expect(otherPost.mock.calls[0][0].html).not.toContain('"cost":7');
  });

  it('does not persist or submit without a stable user identity', () => {
    state.userInfo = { staffName: 'missing id' };
    const { frame, button } = mount();
    emit(frame, { type: 'ace-inline-ready' });
    emit(frame, { type: 'ace-inline-state', value: snapshot });
    button.click();
    expect(localStorage.length).toBe(0);
    expect(send).not.toHaveBeenCalled();
  });

  it('requires a host click and matching validation response; submits once without switching sessions', async () => {
    const { frame, button } = mount();
    emit(frame, { type: 'ace-inline-submit', requestId: 'fake', valid: true, value: snapshot });
    expect(send).not.toHaveBeenCalled();
    emit(frame, { type: 'ace-inline-ready' });
    emit(frame, { type: 'ace-inline-state', value: snapshot });
    const post = vi.spyOn(frame.contentWindow!, 'postMessage');
    button.click();
    const requestId = post.mock.calls.at(-1)![0].requestId;
    emit(frame, { type: 'ace-inline-submit', requestId, valid: true, value: snapshot });
    emit(frame, { type: 'ace-inline-submit', requestId, valid: true, value: snapshot });
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(send.mock.calls[0]).toEqual(['s', expect.stringContaining('"cost":7')]);
    await vi.waitFor(() => expect(button.disabled).toBe(true));
    emit(frame, { type: 'ace-inline-state', value: { ...snapshot, parameters: { cost: 8 } } });
    state.activeSessionId = 'other'; button.click();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('contains component errors without enabling submission or removing the answer', () => {
    const { frame, root, button } = mount();
    emit(frame, { type: 'ace-inline-error' });
    emit(frame, { type: 'ace-inline-ready' });
    emit(frame, { type: 'ace-inline-state', value: snapshot });
    expect(root.isConnected).toBe(true);
    expect(button.disabled).toBe(true);
    expect(root.textContent).toContain('运行出错');
  });

  it('rejects invalid or oversized parameters without storing them', () => {
    const { frame } = mount();
    emit(frame, { type: 'ace-inline-state', value: { parameters: { large: 'x'.repeat(17000) }, controls: {} } });
    expect(localStorage.length).toBe(0);
    expect(document.body.textContent).toContain('未保存');
  });
});
