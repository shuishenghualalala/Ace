import { interactiveOwnerKey } from '../interactive-owner';
import { validValue } from './protocol';
import type { NativeVisual, Values } from './types';
export interface TableView {
    search: string;
    sort: string;
    descending: boolean;
    page: number;
}
export interface ReplyState {
    values: Values;
    views: Record<string, TableView>;
    hidden: Record<string, string[]>;
    sent: string[];
    events: string[];
}
export function replyState(visual: NativeVisual): {
    data: ReplyState;
    save: () => boolean;
    owner: string;
} {
    const owner = interactiveOwnerKey(), spec = visual.spec;
    const key = `ace.reply.v3:${owner}:${visual.sessionId}:${spec.id}:${spec.revision}`;
    const signature = JSON.stringify(spec.parameters ?? []);
    const data: ReplyState = { values: Object.fromEntries((spec.parameters ?? []).map(p => [p.id, structuredClone(p.value)])), views: Object.create(null), hidden: Object.create(null), sent: [], events: [] };
    try {
        const raw = owner ? localStorage.getItem(key) : null;
        if (raw && raw.length <= 64000) {
            const saved = JSON.parse(raw);
            if (saved?.signature === signature && saved?.data && typeof saved.data === 'object') {
                for (const p of spec.parameters ?? [])
                    if (validValue(p, saved.data.values?.[p.id]))
                        data.values[p.id] = saved.data.values[p.id];
                if (saved.revision === spec.revision) {
                    for (const [id, v] of Object.entries(saved.data.views ?? {})) {
                        const view = v as TableView;
                        if (view && typeof view.search === 'string' && view.search.length <= 200 && typeof view.sort === 'string' && typeof view.descending === 'boolean' && Number.isInteger(view.page) && view.page >= 0)
                            data.views[id] = view;
                    }
                    for (const [id, hidden] of Object.entries(saved.data.hidden ?? {}))
                        if (Array.isArray(hidden) && hidden.length <= 8 && hidden.every(v => typeof v === 'string'))
                            data.hidden[id] = hidden;
                    if (Array.isArray(saved.data.sent))
                        data.sent = saved.data.sent.filter((s: unknown) => typeof s === 'string' && s.length <= 18000).slice(-20);
                    if (Array.isArray(saved.data.events))
                        data.events = saved.data.events.filter((s: unknown) => typeof s === 'string' && s.length <= 300).slice(-20);
                }
            }
        }
    }
    catch { /* Malformed/unavailable storage uses defaults. */ }
    return { data, owner, save: () => {
            if (!owner || owner !== interactiveOwnerKey())
                return false;
            try {
                const raw = JSON.stringify({ signature, revision: spec.revision, data });
                if (raw.length > 64000)
                    return false;
                localStorage.setItem(key, raw);
                return true;
            }
            catch {
                return false;
            }
        } };
}
