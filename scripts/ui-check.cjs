/**
 * 控制台交互自检（用真实浏览器点一遍）。
 *
 * 用法：
 *   ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xxx \
 *   ZW_CHROME=/path/to/chrome \
 *   node scripts/ui-check.cjs
 *
 * 依赖 playwright-core（不要求装浏览器，用你指定的 chrome 可执行文件）。
 */
const path = require('node:path');

let chromium = null;
const candidates = [
  'playwright-core',
  'playwright',
  path.join(process.env.HOME || '', '.nvm/versions/node/' + process.version + '/lib/node_modules/playwright-core'),
  '/usr/lib/node_modules/playwright-core',
  '/usr/local/lib/node_modules/playwright-core',
];
for (const p of candidates) {
  try {
    const mod = require(p);
    if (mod && mod.chromium && typeof mod.chromium.launch === 'function') { chromium = mod.chromium; break; }
  } catch (_) { /* 继续试下一个 */ }
}
if (!chromium) {
  console.error('找不到 playwright-core（需要 chromium.launch 可用的版本）。');
  console.error('试过的路径：');
  for (const p of candidates) console.error('  - ' + p);
  console.error('装一个：npm i -g playwright-core');
  process.exit(2);
}

const B = process.env.ZW_CORE || 'http://127.0.0.1:8730';
const T = process.env.ZW_ADMIN_TOKEN || '';
const CHROME = process.env.ZW_CHROME || '/usr/bin/chromium';
const NAME = process.env.ZW_UI_PROFILE || 'ui-check';

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // 先确保有一个干净的环境
  await fetch(`${B}/api/v1/profiles/${NAME}?purge=true`, { method: 'DELETE', headers: { Authorization: 'Bearer ' + T } });
  await fetch(`${B}/api/v1/profiles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + T },
    body: JSON.stringify({ name: NAME, note: '控制台自检' }),
  });

  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.addInitScript(tok => localStorage.setItem('zw_token', tok), T);
  await page.goto(B, { waitUntil: 'networkidle' });
  await page.waitForSelector('#tbody tr', { timeout: 15000 });

  const node = (await page.textContent('#node')).trim();
  const kpis = await page.$$eval('#kpis .kpi', els => els.map(e => e.innerText.replace(/\n/g, ' ')));
  console.log('顶栏：', node);
  console.log('KPI：', kpis.join(' / '));

  const row = page.locator('#tbody tr', { hasText: NAME });
  await row.locator('button', { hasText: '启动' }).click();
  await page.waitForFunction(
    n => [...document.querySelectorAll('#tbody tr')].some(tr => tr.innerText.includes(n) && tr.innerText.includes('运行中')),
    NAME, { timeout: 90000, polling: 500 },
  );
  console.log('✅ 启动');

  await row.locator('button', { hasText: '取 CDP' }).click();
  await page.waitForSelector('#dlg-body pre', { timeout: 20000 });
  const dlg = await page.textContent('#dlg-body');
  if (!dlg.includes('connectOverCDP')) throw new Error('CDP 弹窗里没有 connectOverCDP');
  console.log('✅ 取 CDP：', dlg.split('\n').find(l => l.includes('ws://'))?.trim().slice(0, 80));
  await page.locator('#dlg .df button').click();

  await row.locator('button', { hasText: '日志' }).click();
  await page.waitForSelector('#dlg-body pre', { timeout: 20000 });
  console.log('✅ 日志弹窗');
  await page.locator('#dlg .df button').click();

  await row.locator('button', { hasText: '停止' }).click();
  await page.waitForFunction(
    n => [...document.querySelectorAll('#tbody tr')].some(tr => tr.innerText.includes(n) && tr.innerText.includes('已停止')),
    NAME, { timeout: 90000, polling: 500 },
  );
  console.log('✅ 停止');

  await page.screenshot({ path: 'ui-console.png' });
  await browser.close();

  if (errors.length) {
    console.error('❌ 前端报错：', errors.slice(0, 5));
    process.exit(1);
  }
  console.log('✅ 无前端报错');
  console.log('=== 控制台交互自检通过（截图 ui-console.png）===');
})().catch(e => { console.error('❌ 失败：', e.message); process.exit(1); });
