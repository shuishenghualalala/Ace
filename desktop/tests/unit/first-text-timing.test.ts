/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import './helpers/mock-chat-controller-deps';
import { beginFirstText, observeFirstText } from '../../src/ui/first-text-timing';
import { _resetTurnDurationTickerForTests, applyChunk, renderChat } from '../../src/ui/features/chat-controller';
import { openTurnForRequest } from '../../src/ui/features/session-busy';
import { dumpStreamDebugLog } from '../../src/ui/stream-debug';
import { appendSessionMessage, ensureSessionBook as bookFor, patchBook, setBusy } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, sessionStore } from '../../src/ui/stores/stores';

const received = 'ttft-first-text-received';
const accepted = 'ttft-first-text-accepted';
const mounted = 'ttft-first-text-mounted';
const opportunity = 'ttft-first-paint-opportunity';
let sid: string;
let frames: FrameRequestCallback[];

function entries() {
  return dumpStreamDebugLog().filter(row => row.event.startsWith('ttft-') && row.detail?.sid === sid);
}
function events() {
  return entries().map(row => row.event);
}
function delta(requestId = 'request', text = 'Synthetic visible reply', sequence = 1) {
  applyChunk({ kind: 'delta', body: { text }, request_id: requestId, session_id: sid, is_final: false, sequence });
}
function frame() {
  const callbacks = frames.splice(0);
  callbacks.forEach(callback => callback(performance.now()));
}

beforeEach(() => {
  __resetAllStoresForTest();
  sid = crypto.randomUUID();
  sessionStore.set({ activeSessionId: sid });
  document.body.innerHTML = '<section id="welcome-panel"></section><section id="chat-panel"><div id="chat-messages"></div></section>';
  localStorage.setItem('mm:debug-stream', '1');
  vi.spyOn(console, 'info').mockImplementation(() => {});
  frames = [];
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  appendSessionMessage(sid, { id: sid + '-user', role: 'user', content: 'prompt', timestamp: Date.now() });
  openTurnForRequest(sid, 'request');
  setBusy(sid, true);
  beginFirstText(sid, 'request');
});

afterEach(() => {
  localStorage.removeItem('mm:debug-stream');
  while (frames.length) frame();
  _resetTurnDurationTickerForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('desktop first-text diagnostics', () => {
  it('records receipt once and never calls a sealed delta accepted or mounted', () => {
    patchBook(sid, { turnSealed: true, acceptingNewRequest: false });
    delta();
    delta();
    renderChat();
    expect(events()).toEqual([received]);
  });

  it('observes the same nonempty assistant body after a full render and two frame opportunities', () => {
    delta();
    expect(events()).toEqual([received, accepted]);
    renderChat();
    const id = bookFor(sid).assistantId!;
    const body = document.querySelector(`[data-text-for="${id}"]`);
    expect(body?.textContent).toContain('Synthetic visible reply');
    expect(events()).toEqual([received, accepted, mounted]);
    expect(entries()[2]?.detail).toMatchObject({ source: 'render', assistantId: id });
    frame();
    expect(events()).not.toContain(opportunity);
    frame();
    expect(events()).toEqual([received, accepted, mounted, opportunity]);
    delta('request', 'Synthetic visible reply', 1);
    renderChat(); frame(); frame();
    expect(events()).toEqual([received, accepted, mounted, opportunity]);
    expect(JSON.stringify(entries())).not.toContain('Synthetic visible reply');
  });

  it('waits for Gateway fragment repair and an attributed request before accepting text', () => {
    expect(applyChunk({ kind: 'delta', session_id: sid, request_id: 'request', is_final: false, sequence: 1,
      body: { text: 'fragment', delta_parts: [] } })).toBe('repair');
    expect(events()).toEqual([received]);
    applyChunk({ kind: 'delta', session_id: sid, is_final: false, sequence: 2, body: { text: 'uncorrelated' } });
    renderChat();
    expect(events()).toEqual([received]);
  });

  it('observes an actual streaming patch after it writes the mounted body', () => {
    applyChunk({ kind: 'thinking', session_id: sid, request_id: 'request', is_final: false, sequence: 0,
      body: { text: 'Thinking without response text' } });
    while (frames.length) frame();
    delta();
    frame();
    expect(entries().find(row => row.event === mounted)?.detail).toMatchObject({ source: 'patch' });
    expect(document.querySelector(`[data-text-for="${bookFor(sid).assistantId}"]`)?.textContent).toContain('Synthetic visible reply');
  });

  it('rebuilds a turn whose indexed text container was detached before the first delta', () => {
    applyChunk({ kind: 'thinking', session_id: sid, request_id: 'request', is_final: false, sequence: 0,
      body: { text: 'Thinking without response text' } });
    while (frames.length) frame();
    const selector = `[data-text-for="${bookFor(sid).assistantId}"]`;
    const placeholder = document.querySelector(selector);
    expect(placeholder).not.toBeNull();
    placeholder!.remove();
    delta();
    expect(events()).toEqual([received, accepted]);
    frame();
    expect(document.querySelector(selector)?.textContent).toContain('Synthetic visible reply');
    expect(entries().find(row => row.event === mounted)?.detail).toMatchObject({ source: 'render' });
  });

  it('requires a connected visible body and retires frame callbacks when the local turn changes', () => {
    const root = document.getElementById('chat-messages')!;
    delta();
    observeFirstText(sid, 'patch', () => root);
    expect(events()).toEqual([received, accepted]);
    const body = document.createElement('div');
    body.dataset.textFor = bookFor(sid).assistantId!;
    body.textContent = 'Mounted';
    root.append(body);
    body.hidden = true;
    observeFirstText(sid, 'patch', () => root);
    expect(events()).toEqual([received, accepted]);
    body.hidden = false;
    observeFirstText(sid, 'patch', () => root);
    expect(events()).toEqual([received, accepted, mounted]);
    beginFirstText(sid, 'other');
    frame(); frame();
    expect(events()).not.toContain(opportunity);
  });

  it('keeps a background receipt and accepted update distinct from a foreground observation', () => {
    sessionStore.set({ activeSessionId: 'other-session' });
    delta();
    renderChat(); frame(); frame();
    expect(events()).toEqual([received, accepted]);
    expect(entries()[1]?.detail?.foreground).toBe(false);
    sessionStore.set({ activeSessionId: sid });
    renderChat();
    expect(events()).toEqual([received, accepted, mounted]);
    sessionStore.set({ activeSessionId: 'other-session' });
    frame(); frame();
    expect(events()).not.toContain(opportunity);
  });

  it('does not report a frame after its element is removed or an account reuses the session', () => {
    delta(); renderChat();
    frame();
    const body = document.querySelector(`[data-text-for="${bookFor(sid).assistantId}"]`);
    body!.remove();
    frame();
    expect(events()).toEqual([received, accepted, mounted]);
    authStore.set({ isLoggedIn: true, userInfo: { uid: 'another-owner' } });
    renderChat(); frame(); frame();
    expect(events()).not.toContain(opportunity);
  });

  it('bounds measurements for abandoned sessions and does not start measurements with diagnostics off', () => {
    for (let i = 0; i < 80; i++) beginFirstText(sid + i, 'other');
    delta();
    expect(events()).toEqual([]);
    localStorage.removeItem('mm:debug-stream');
    beginFirstText(sid, 'request');
    localStorage.setItem('mm:debug-stream', '1');
    delta(); renderChat();
    expect(events()).toEqual([]);
  });
});
