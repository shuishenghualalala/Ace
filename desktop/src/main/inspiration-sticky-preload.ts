import { ipcRenderer } from 'electron';

function mountStickyChrome(): void {
  if (document.getElementById('ace-inspiration-sticky-chrome')) return;

  const pageStyle = document.createElement('style');
  pageStyle.dataset.aceStickyChrome = 'true';
  pageStyle.textContent = `
    html, body {
      width: 100%;
      min-width: 0;
      height: 100%;
      min-height: 0;
      overflow: hidden;
      border-radius: 18px;
    }
    @media (prefers-reduced-transparency: reduce) {
      html, body { border-radius: 12px; }
    }
  `;
  document.head.appendChild(pageStyle);

  const host = document.createElement('div');
  host.id = 'ace-inspiration-sticky-chrome';
  host.style.cssText = 'all:initial!important;position:fixed!important;top:10px!important;right:10px!important;z-index:2147483647!important;';
  const shadow = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style');
  style.textContent = `
    * { box-sizing: border-box; }
    .chrome {
      display: flex;
      height: 34px;
      align-items: center;
      gap: 4px;
      padding: 4px;
      border: 1px solid rgba(255, 255, 255, .62);
      border-radius: 12px;
      background: rgba(31, 35, 43, .78);
      box-shadow: 0 8px 24px rgba(0, 0, 0, .18);
      color: #fff;
      -webkit-app-region: drag;
      backdrop-filter: blur(18px) saturate(145%);
      user-select: none;
    }
    .grip {
      display: grid;
      width: 24px;
      height: 24px;
      place-items: center;
      color: rgba(255, 255, 255, .76);
      font: 700 13px/1 system-ui, sans-serif;
      letter-spacing: -1px;
      cursor: move;
    }
    button {
      display: grid;
      width: 26px;
      height: 26px;
      place-items: center;
      padding: 0;
      border: 0;
      border-radius: 8px;
      outline: none;
      background: transparent;
      color: inherit;
      font: 500 19px/1 system-ui, sans-serif;
      cursor: pointer;
      -webkit-app-region: no-drag;
    }
    button:hover { background: rgba(255, 255, 255, .16); }
    button:focus-visible { box-shadow: 0 0 0 2px #8db2ff; }
    @media (prefers-reduced-transparency: reduce) {
      .chrome { background: #242832; backdrop-filter: none; }
    }
  `;
  const chrome = document.createElement('div');
  chrome.className = 'chrome';
  chrome.setAttribute('aria-label', '灵感便利贴控制栏');
  const grip = document.createElement('span');
  grip.className = 'grip';
  grip.textContent = '⠿';
  grip.title = '拖动便利贴';
  const close = document.createElement('button');
  close.type = 'button';
  close.textContent = '×';
  close.title = '取消固定';
  close.setAttribute('aria-label', '取消固定');
  close.addEventListener('click', () => ipcRenderer.send('inspiration:sticky-close'));
  chrome.append(grip, close);
  shadow.append(style, chrome);
  document.documentElement.appendChild(host);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountStickyChrome, { once: true });
} else {
  mountStickyChrome();
}
