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
    #ace-inspiration-sticky-drag-strip {
      position: fixed !important;
      z-index: 2147483646 !important;
      top: 0 !important;
      right: 116px !important;
      left: 0 !important;
      height: 12px !important;
      cursor: move !important;
      -webkit-app-region: drag !important;
      user-select: none !important;
    }
    #ace-inspiration-sticky-chrome {
      all: initial !important;
      position: fixed !important;
      z-index: 2147483647 !important;
      top: 10px !important;
      right: 10px !important;
      display: flex !important;
      height: 36px !important;
      align-items: center !important;
      gap: 4px !important;
      padding: 4px !important;
      border: 1px solid rgba(255, 255, 255, .62) !important;
      border-radius: 12px !important;
      background: rgba(31, 35, 43, .82) !important;
      box-shadow: 0 8px 24px rgba(0, 0, 0, .18) !important;
      color: #fff !important;
      font: 600 12px/1 system-ui, sans-serif !important;
      backdrop-filter: blur(18px) saturate(145%) !important;
      -webkit-app-region: drag !important;
      user-select: none !important;
    }
    #ace-inspiration-sticky-chrome .ace-sticky-grip {
      display: flex !important;
      height: 28px !important;
      align-items: center !important;
      gap: 5px !important;
      padding: 0 8px !important;
      color: rgba(255, 255, 255, .9) !important;
      cursor: move !important;
      -webkit-app-region: drag !important;
    }
    #ace-inspiration-sticky-chrome .ace-sticky-grip-mark {
      color: rgba(255, 255, 255, .68) !important;
      font-size: 14px !important;
      letter-spacing: -1px !important;
    }
    #ace-inspiration-sticky-chrome button {
      all: initial !important;
      display: grid !important;
      width: 28px !important;
      height: 28px !important;
      place-items: center !important;
      border-radius: 8px !important;
      color: #fff !important;
      font: 500 19px/1 system-ui, sans-serif !important;
      cursor: pointer !important;
      -webkit-app-region: no-drag !important;
    }
    #ace-inspiration-sticky-chrome button:hover {
      background: rgba(255, 255, 255, .16) !important;
    }
    #ace-inspiration-sticky-chrome button:focus-visible {
      box-shadow: 0 0 0 2px #8db2ff !important;
    }
    @media (prefers-reduced-transparency: reduce) {
      html, body { border-radius: 12px; }
      #ace-inspiration-sticky-chrome {
        background: #242832 !important;
        backdrop-filter: none !important;
      }
    }
  `;
  document.head.appendChild(pageStyle);

  const dragStrip = document.createElement('div');
  dragStrip.id = 'ace-inspiration-sticky-drag-strip';
  dragStrip.title = '拖动便利贴';
  dragStrip.setAttribute('aria-hidden', 'true');
  const host = document.createElement('div');
  host.id = 'ace-inspiration-sticky-chrome';
  host.setAttribute('aria-label', '灵感便利贴控制栏');
  const grip = document.createElement('span');
  grip.className = 'ace-sticky-grip';
  grip.title = '拖动便利贴';
  const gripMark = document.createElement('span');
  gripMark.className = 'ace-sticky-grip-mark';
  gripMark.textContent = '⠿';
  gripMark.setAttribute('aria-hidden', 'true');
  const gripLabel = document.createElement('span');
  gripLabel.textContent = '拖动';
  grip.append(gripMark, gripLabel);
  const close = document.createElement('button');
  close.type = 'button';
  close.textContent = '×';
  close.title = '取消固定并关闭';
  close.setAttribute('aria-label', '取消固定并关闭');
  close.addEventListener('click', () => ipcRenderer.send('inspiration:sticky-close'));
  host.append(grip, close);
  document.documentElement.append(dragStrip, host);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountStickyChrome, { once: true });
} else {
  mountStickyChrome();
}
