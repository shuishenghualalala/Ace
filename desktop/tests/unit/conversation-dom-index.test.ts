/** @vitest-environment happy-dom */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getLiveConversationTurn,
  getLiveTeamLabel,
  submitConversationInteraction,
  syncConversationDom,
} from '../../src/ui/features/conversation-dom-index';
import { authStore } from '../../src/ui/stores/auth-store';

function turn(id: string, { team = false, live = true } = {}) {
  const root = document.createElement('article');
  root.className = team ? 'msg team-internal' : 'msg';
  root.dataset.messageId = id;
  if (live) root.dataset.streaming = 'true';
  const label = root.appendChild(document.createElement('span'));
  label.className = 'msg__fold-label';
  const text = root.appendChild(document.createElement('div'));
  text.dataset.textFor = id;
  const marker = root.appendChild(document.createElement('div'));
  marker.dataset.thinkingFor = id;
  const thinking = marker.appendChild(document.createElement('div'));
  thinking.className = 'process-timeline__thinking';
  return { root, label, text, thinking };
}

function activity(id: string) {
  const row = document.createElement('article');
  const card = row.appendChild(document.createElement('div'));
  card.className = 'interaction-card';
  card.dataset.interactionId = id;
  const control = card.appendChild(document.createElement('button'));
  control.dataset.interactionSubmit = '1';
  return { row, card, control };
}

function completion(id: string) {
  const row = document.createElement('article');
  row.dataset.interactionCompletion = id;
  return row;
}

function container(...rows: HTMLElement[]) {
  const root = document.createElement('section');
  root.append(...rows);
  document.body.append(root);
  return root;
}

beforeEach(() => {
  document.body.replaceChildren();
  authStore.set({ isLoggedIn: true, userInfo: { staffCode: 'f1-test-owner' } });
});

describe('mounted conversation indexes', () => {
  it('finds exact live text, thinking and timers without sweeping earlier rows', () => {
    const history = turn('finished', { live: false });
    const other = turn('background');
    const active = turn('latest');
    const team = turn('team-member', { team: true });
    const root = container(history.root, other.root, active.root, team.root);
    syncConversationDom(root, 'session-live', { added: [history.root, other.root, active.root, team.root], removed: [] });
    const rootScan = vi.spyOn(root, 'querySelectorAll');
    const oldScan = vi.spyOn(history.root, 'querySelectorAll');

    expect(getLiveConversationTurn(root, 'session-live', 'latest')).toEqual(active);
    expect(getLiveConversationTurn(root, 'session-live', 'background')?.text).toBe(other.text);
    expect(getLiveConversationTurn(root, 'session-live', 'finished')).toBeUndefined();
    expect(getLiveTeamLabel(root, 'session-live', 'team-member')).toBe(team.label);

    const patched = turn('latest');
    active.root.replaceWith(patched.root);
    syncConversationDom(root, 'session-live', { added: [patched.root], removed: [active.root] });
    expect(getLiveConversationTurn(root, 'session-live', 'latest')?.text).toBe(patched.text);
    expect(rootScan).not.toHaveBeenCalled();
    expect(oldScan).not.toHaveBeenCalled();
  });

  it('fences stale nodes and resets lookup when a container switches conversation', () => {
    const first = turn('same-id');
    const root = container(first.root);
    syncConversationDom(root, 'old-session', { added: [first.root], removed: [] });
    first.root.remove();
    expect(getLiveConversationTurn(root, 'old-session', 'same-id')).toBeUndefined();

    const replacement = turn('same-id');
    root.append(replacement.root);
    syncConversationDom(root, 'new-session', { added: [replacement.root], removed: [], reset: true });
    expect(getLiveConversationTurn(root, 'old-session', 'same-id')).toBeUndefined();
    expect(getLiveConversationTurn(root, 'new-session', 'same-id')?.root).toBe(replacement.root);

    authStore.set({ userInfo: { staffCode: 'another-owner' } });
    expect(getLiveConversationTurn(root, 'new-session', 'same-id')).toBeUndefined();
  });

  it('switches only the active activity on order, result completion and submission', () => {
    const older = activity('older-activity');
    const latest = activity('latest-activity');
    const root = container(older.row, latest.row);
    syncConversationDom(root, 'interaction-session', { added: [older.row, latest.row], removed: [] });
    expect(older.control.disabled).toBe(true);
    expect(latest.control.disabled).toBe(false);

    const previousControlScan = vi.spyOn(latest.card, 'querySelectorAll');
    root.insertBefore(latest.row, older.row);
    syncConversationDom(root, 'interaction-session', { added: [], removed: [], reordered: true });
    expect(older.control.disabled).toBe(false);
    expect(latest.control.disabled).toBe(true);
    expect(submitConversationInteraction(root, 'interaction-session', 'older-activity', latest.control)).toBe(false);

    const completed = completion('older-activity');
    root.append(completed);
    syncConversationDom(root, 'interaction-session', { added: [completed], removed: [] });
    expect(older.control.disabled).toBe(true);
    expect(latest.control.disabled).toBe(false);
    expect(submitConversationInteraction(root, 'interaction-session', 'latest-activity', latest.control)).toBe(true);
    expect(latest.control.disabled).toBe(true);
    expect(previousControlScan).not.toHaveBeenCalled();
  });

  it('shares a submission across mounts for one owner/session without affecting the next account', () => {
    const first = activity('reused-activity');
    const alternate = activity('reused-activity');
    const root = container(first.row);
    const otherRoot = container(alternate.row);
    syncConversationDom(root, 'shared', { added: [first.row], removed: [] });
    syncConversationDom(otherRoot, 'shared', { added: [alternate.row], removed: [] });
    expect(submitConversationInteraction(root, 'shared', 'reused-activity', first.control)).toBe(true);
    expect(submitConversationInteraction(otherRoot, 'shared', 'reused-activity', alternate.control)).toBe(false);

    authStore.set({ userInfo: { staffCode: 'new-account' } });
    syncConversationDom(otherRoot, 'shared', { added: [], removed: [] });
    expect(alternate.control.disabled).toBe(true);
    const third = activity('reused-activity');
    otherRoot.replaceChildren(third.row);
    syncConversationDom(otherRoot, 'shared', { added: [third.row], removed: [], reset: true });
    expect(third.control.disabled).toBe(false);
  });

  it('keeps authenticated UID-only aliases isolated from another owner', () => {
    authStore.set({ userInfo: { uid: 'uid-one' } });
    const one = activity('alias-activity');
    const root = container(one.row);
    syncConversationDom(root, 'alias-session', { added: [one.row], removed: [] });
    expect(submitConversationInteraction(root, 'alias-session', 'alias-activity', one.control)).toBe(true);
    authStore.set({ userInfo: { uid: 'uid-two' } });
    const two = activity('alias-activity');
    one.row.replaceWith(two.row);
    syncConversationDom(root, 'alias-session', { added: [two.row], removed: [one.row] });
    expect(two.control.disabled).toBe(false);
  });
});
