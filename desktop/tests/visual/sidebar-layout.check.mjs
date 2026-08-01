/**
 * 侧边栏布局溢出验证（CSS 调整后的视觉/几何回归测试）。
 *
 * 模拟截图场景：52px 窄轨、8 个导航项全显示、已登录（显示用户头像），
 * 在 800px / 700px 两种窗口高度下校验：
 *   1. sidebar-nav 不出现内部滚动（scrollHeight <= clientHeight）
 *   2. 顶部品牌 Logo 完整可见（不被上边缘裁掉）
 *   3. 底部用户头像完整可见（不超出侧栏下边缘）
 * 并截图 sidebar 区域供人工核对。
 *
 * 运行：node tests/visual/sidebar-layout.check.mjs
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const indexHtml = path.resolve(__dirname, '../../assets/index.html');
const shotDir = __dirname;

const results = [];

for (const height of [800, 700]) {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height } });
  await page.goto(`file://${indexHtml}`);
  await page.waitForTimeout(500);

  // 模拟最坏情况：智能体按钮显示 + 已登录显示头像
  await page.evaluate(() => {
    document.querySelector('[data-tab="agents"]')?.removeAttribute('hidden');
    const user = document.getElementById('user-section');
    if (user) user.style.display = 'flex';
    const login = document.getElementById('login-section');
    if (login) login.style.display = 'none';
  });
  await page.waitForTimeout(100);

  const metrics = await page.evaluate(() => {
    const sidebar = document.getElementById('sidebar');
    const nav = sidebar.querySelector('.sidebar-nav');
    const brand = sidebar.querySelector('.brand-logo-icon');
    const avatar = sidebar.querySelector('.user-avatar-btn');
    const sRect = sidebar.getBoundingClientRect();
    const bRect = brand.getBoundingClientRect();
    const aRect = avatar.getBoundingClientRect();
    return {
      navScrollable: nav.scrollHeight > nav.clientHeight + 1,
      navScrollHeight: nav.scrollHeight,
      navClientHeight: nav.clientHeight,
      brandTopOffset: Math.round(bRect.top - sRect.top),
      avatarBottomOverflow: Math.round(aRect.bottom - sRect.bottom),
      sidebarHeight: Math.round(sRect.height),
    };
  });

  const sidebarEl = await page.locator('#sidebar');
  await sidebarEl.screenshot({ path: path.join(shotDir, `sidebar-${height}.png`) });

  const pass =
    !metrics.navScrollable &&
    metrics.brandTopOffset >= 0 &&
    metrics.avatarBottomOverflow <= 0;
  results.push({ height, pass, ...metrics });
  await browser.close();
}

let failed = false;
for (const r of results) {
  const status = r.pass ? 'PASS' : 'FAIL';
  if (!r.pass) failed = true;
  console.log(
    `[${status}] 窗口高度 ${r.height}px | sidebar ${r.sidebarHeight}px | ` +
      `nav 滚动=${r.navScrollable} (${r.navScrollHeight}/${r.navClientHeight}) | ` +
      `Logo 顶距=${r.brandTopOffset}px | 头像底部溢出=${r.avatarBottomOverflow}px`,
  );
}
process.exit(failed ? 1 : 0);
