/**
 * 自动化宿主窗口：让**用户看不见的**标签页仍然能被 Playwright 驱动。
 *
 * ## 问题
 *
 * Playwright 判断"元素可以点"要过 actionability：可见、**稳定**、能收事件、可用。
 * 其中"稳定"靠 `requestAnimationFrame` 比较相邻两帧的包围盒。而 Electron 里一个
 * 既不可见、又不挂在任何窗口上的 `WebContentsView` 既不出帧也没有视口，于是：
 *
 * - rAF 不推进 → 所有点击卡在 actionability 直到超时；
 * - 没有视口   → 报 `Element is outside of the viewport`，截图报 0 宽度。
 *
 * 注意这**不是** Playwright 比手写 CDP 弱。手写 `Input.dispatchMouseEvent` 之所以
 * "能点"，正是因为它跳过了这些校验 —— 那才是动态页面上点错、点空的根源。
 *
 * ## 三个条件（实测逐项隔离，缺一不可，且都不要求窗口可见）
 *
 * 1. `Emulation.setFocusEmulationEnabled` → rAF 才推进。与 view 是否可见无关。
 * 2. `view.setVisible(true)`              → 才有视口。这里的"可见"是相对所在窗口而言。
 * 3. view 必须 `addChildView` 挂到某个 `BrowserWindow` 上 —— 游离的 view 即使前两条
 *    都满足也不行。
 *
 * 本文件负责 2 和 3：提供一个**永不 show 的**宿主窗口。用户全程看不到任何东西。
 * 条件 1 在 `playwright-engine.ts` 里随会话建立时下发。
 *
 * ## 与面板的关系
 *
 * 同一个 view 可以在宿主窗口与面板窗口之间来回移动：实测 debugger 保持 attached、
 * targetId 不变、页面状态不丢（不触发重载）、焦点模拟无需重设。所以"AI 在后台跑 /
 * 用户开面板看"是同一个 view 的两个挂载点，不需要双份视图。
 */

import { BrowserWindow } from 'electron';

import type { WebContentsView } from 'electron';

/** 宿主窗口尺寸即自动化默认视口。与面板常用尺寸保持一致，避免移动时布局跳变。 */
export const AUTOMATION_VIEWPORT = { width: 1280, height: 800 } as const;

export class AutomationHost {
  private window: BrowserWindow | null = null;
  private readonly mounted = new Set<WebContentsView>();

  /**
   * 把 view 挂到隐藏宿主窗口上并置为可见。
   *
   * 幂等：重复调用只做一次挂载。若 view 当前挂在面板窗口上，调用方需先摘除
   * （`BrowserWindow.contentView.removeChildView`），否则 Electron 会把它从旧父节点移走。
   */
  mount(view: WebContentsView): void {
    if (view.webContents.isDestroyed()) return;
    const host = this.ensureWindow();
    host.contentView.addChildView(view);
    view.setBounds({ x: 0, y: 0, ...AUTOMATION_VIEWPORT });
    // 条件 2：相对宿主窗口可见。宿主窗口本身永远 show:false。
    view.setVisible(true);
    this.mounted.add(view);
  }

  /** 从宿主窗口摘除（移交面板或标签页关闭时）。 */
  unmount(view: WebContentsView): void {
    if (!this.mounted.delete(view)) return;
    if (!this.window || this.window.isDestroyed()) return;
    if (view.webContents.isDestroyed()) return;
    this.window.contentView.removeChildView(view);
  }

  /** 当前挂载数，测试与诊断用。 */
  get size(): number {
    return this.mounted.size;
  }

  dispose(): void {
    this.mounted.clear();
    if (this.window && !this.window.isDestroyed()) this.window.destroy();
    this.window = null;
  }

  private ensureWindow(): BrowserWindow {
    if (this.window && !this.window.isDestroyed()) return this.window;
    this.window = new BrowserWindow({
      ...AUTOMATION_VIEWPORT,
      // 条件 3 的载体。show 恒为 false —— 这个窗口从创建到销毁都不会出现在
      // 屏幕上，也不进任务栏。它存在的唯一理由是给 view 一个合成上下文。
      show: false,
      skipTaskbar: true,
      // 即便某条路径误调 show()，也不该抢走用户焦点。
      focusable: false,
      webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
    });
    this.window.on('closed', () => {
      this.window = null;
      this.mounted.clear();
    });
    return this.window;
  }
}
