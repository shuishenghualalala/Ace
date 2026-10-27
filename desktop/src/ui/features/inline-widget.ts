import { state } from '../state';
import { interactiveOwnerKey } from './interactive-owner';

export interface InlineWidget {
  key: string;
  sessionId: string;
  widgetId: string;
  title: string;
  html: string;
  resourceRevision: number;
}
type Snapshot = { parameters: Record<string, unknown>; controls: Record<string, unknown> };
type Submit = (sessionId: string, text: string) => Promise<boolean>;
interface WidgetRecord {
  widget: InlineWidget; owner: string; storageKey: string; snapshot: Snapshot;
  frame: HTMLIFrameElement; button: HTMLButtonElement; status: HTMLElement;
  pending: string; submitting: boolean; lastSent: string; ready: boolean; failed: boolean;
}
const records = new WeakMap<HTMLElement, WidgetRecord>();
const recordsBySource = new WeakMap<Window, WidgetRecord>();
let submit: Submit | undefined;
let bound = false;
const bytes = (value: string): number => new TextEncoder().encode(value).length;

function snapshotOf(value: unknown): Snapshot | null {
  try {
    if (!value || typeof value !== 'object' || bytes(JSON.stringify(value)) > 16_000) return null;
    const s = value as Snapshot;
    if (!s.parameters || typeof s.parameters !== 'object' || Array.isArray(s.parameters)
      || !s.controls || typeof s.controls !== 'object' || Array.isArray(s.controls)) return null;
    return JSON.parse(JSON.stringify(s)) as Snapshot;
  } catch { return null; }
}

/** Self-contained, opaque-origin document. No desktop bridge, remote assets or network. */
export function inlineDocument(html: string, snapshot: Snapshot): string {
  const initial = JSON.stringify(snapshot).replace(/</g, '\\u003c');
  const bridge = `<script>(()=>{
    let saved=${initial};
    const post=(type,extra={})=>parent.postMessage({type,...extra},'*');
    const fields=()=>[...document.querySelectorAll('input,select,textarea')].filter(e=>e.type!=='password'&&e.type!=='file'&&(e.name||e.id));
    const controls=()=>Object.fromEntries(fields().map(e=>[e.id||e.name,e.type==='checkbox'||e.type==='radio'?e.checked:e.value]));
    const snapshot=()=>({parameters:saved.parameters,controls:controls()});
    const save=()=>post('ace-inline-state',{value:snapshot()});
    window.AceInteractive={get state(){return saved.parameters},setState(value){
      if(!value||typeof value!=='object'||Array.isArray(value))throw Error('state must be an object');
      saved.parameters=value;save();
    }};
    addEventListener('error',()=>post('ace-inline-error'));
    addEventListener('unhandledrejection',()=>post('ace-inline-error'));
    addEventListener('input',save);addEventListener('change',save);
    addEventListener('message',e=>{
      if(e.source!==parent||e.data?.type!=='ace-inline-validate')return;
      const valid=fields().every(el=>el.reportValidity());
      post('ace-inline-submit',{requestId:e.data.requestId,valid,value:snapshot()});
    });
    addEventListener('DOMContentLoaded',()=>{
      for(const e of fields()){
        const key=e.id||e.name;
        if(!Object.prototype.hasOwnProperty.call(saved.controls,key))continue;
        if(e.type==='checkbox'||e.type==='radio')e.checked=!!saved.controls[key];else e.value=saved.controls[key];
        e.dispatchEvent(new Event('input',{bubbles:true}));
      }
      const resize=()=>post('ace-inline-size',{height:document.documentElement.scrollHeight});
      new ResizeObserver(resize).observe(document.body);resize();save();post('ace-inline-ready');
    });
  })();</script>`;
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"><style>html{color-scheme:light dark}body{margin:0;padding:12px;box-sizing:border-box;font:14px system-ui}*{box-sizing:border-box}</style>${bridge}</head><body>${html}</body></html>`;
}

export function renderInlineWidget(widget: InlineWidget): HTMLElement {
  const owner = interactiveOwnerKey();
  const storageKey = `ace.inline.v1:${owner}:${widget.key}`;
  let snapshot: Snapshot = { parameters: {}, controls: {} };
  try { snapshot = snapshotOf(JSON.parse(localStorage.getItem(storageKey) || 'null')) || snapshot; } catch { /* Empty state. */ }
  const root = document.createElement('article');
  root.className = 'inline-widget';
  const heading = document.createElement('strong'); heading.textContent = widget.title;
  const frame = document.createElement('iframe');
  frame.title = widget.title; frame.setAttribute('sandbox', 'allow-scripts');
  frame.referrerPolicy = 'no-referrer';
  const footer = document.createElement('div'); footer.className = 'inline-widget__footer';
  const button = document.createElement('button'); button.type = 'button';
  button.textContent = '用当前参数继续分析'; button.disabled = true;
  const status = document.createElement('span'); status.setAttribute('role', 'status');
  status.textContent = '正在加载交互组件…';
  footer.append(button, status); root.append(heading, frame, footer);
  const record = { widget, owner, storageKey, snapshot, frame, button, status, pending: '', submitting: false, lastSent: '', ready: false, failed: false };
  records.set(root, record);
  button.addEventListener('click', () => {
    if (!record.ready || record.failed || record.pending || record.submitting || !root.isConnected) return;
    if (!owner || owner !== interactiveOwnerKey() || state.activeSessionId !== widget.sessionId) {
      status.textContent = '请在此组件所属的聊天中继续分析'; return;
    }
    record.pending = crypto.randomUUID(); button.disabled = true;
    status.textContent = '正在检查参数…';
    frame.contentWindow?.postMessage({ type: 'ace-inline-validate', requestId: record.pending }, '*');
    const requestId = record.pending;
    window.setTimeout(() => {
      if (record.pending !== requestId) return;
      record.pending = ''; button.disabled = false; status.textContent = '组件未响应，请重试';
    }, 5000);
  });
  return root;
}

export function hydrateInlineWidgets(container: HTMLElement): void {
  for (const root of container.querySelectorAll<HTMLElement>('.inline-widget')) {
    const r = records.get(root);
    if (!r || r.frame.hasAttribute('src')) continue;
    r.frame.src = `ace-site://${r.widget.widgetId}/?inline_shell=1`;
    if (r.frame.contentWindow) recordsBySource.set(r.frame.contentWindow, r);
    window.setTimeout(() => {
      if (root.isConnected && !r.ready && !r.failed) r.status.textContent = '组件未能加载，请重新打开此聊天';
    }, 10000);
  }
}

export function bindInlineWidgets(handler: Submit): void {
  submit = handler;
  if (bound) return;
  bound = true;
  window.addEventListener('message', (event) => {
    const r = event.source ? recordsBySource.get(event.source as Window) : undefined;
    if (!r || event.source !== r.frame.contentWindow || !r.owner || r.owner !== interactiveOwnerKey()) return;
    const data = event.data;
    if (data?.type === 'ace-inline-shell-ready') {
      r.frame.contentWindow?.postMessage({ type: 'ace-inline-init', html: inlineDocument(r.widget.html, r.snapshot) }, '*');
    } else if (data?.type === 'ace-inline-error') {
      r.failed = true; r.button.disabled = true;
      r.status.textContent = '组件运行出错，可让 Agent 修复后重新展示';
    } else if (data?.type === 'ace-inline-ready' && !r.failed) {
      r.ready = true; r.button.disabled = false; r.status.textContent = '参数变动自动保存到此设备';
    } else if (data?.type === 'ace-inline-size' && Number.isFinite(data.height)) {
      r.frame.style.height = `${Math.max(180, Math.min(720, data.height))}px`;
    } else if (data?.type === 'ace-inline-state') {
      const snapshot = snapshotOf(data.value);
      if (!snapshot) { r.status.textContent = '参数过大或格式无效，未保存'; return; }
      r.snapshot = snapshot;
      try { localStorage.setItem(r.storageKey, JSON.stringify(snapshot)); }
      catch { r.status.textContent = '本机存储已满，当前参数未保存'; }
      if (r.ready && !r.failed && !r.pending && !r.submitting) r.button.disabled = JSON.stringify(snapshot) === r.lastSent;
    } else if (data?.type === 'ace-inline-submit' && r.pending && data.requestId === r.pending) {
      r.pending = '';
      const snapshot = snapshotOf(data.value);
      if (r.failed || !data.valid || !snapshot || state.activeSessionId !== r.widget.sessionId) {
        r.button.disabled = false; r.status.textContent = '请检查输入，并在所属聊天中提交'; return;
      }
      const serialized = JSON.stringify(snapshot);
      if (serialized === r.lastSent || r.submitting) return;
      r.submitting = true;
      r.status.textContent = '正在提交…';
      const text = `请根据交互组件「${r.widget.title}」的当前参数继续分析。以下 JSON 是用户调整的数据：\n${serialized}`;
      void Promise.resolve().then(() => submit?.(r.widget.sessionId, text)).then((sent) => {
        if (sent) r.lastSent = serialized;
        r.status.textContent = sent ? '参数已提交，可修改后再次分析' : '提交失败，请重试';
      }).catch(() => { r.status.textContent = '提交失败，请重试'; }).finally(() => {
        r.submitting = false; r.button.disabled = r.failed || JSON.stringify(r.snapshot) === r.lastSent;
      });
    }
  });
}
