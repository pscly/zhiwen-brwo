/**
 * 网关接管自检：用 Playwright 通过**网关地址**连浏览器，这验证三件最容易出错的事：
 *   1. 发现接口里的回环地址被正确重写成网关地址；
 *   2. 带令牌的 ws 能真的连上（--remote-allow-origins 与令牌校验都对）；
 *   3. 连上之后能读写页面、读写 cookie，且与直连本机端口看到的是同一个浏览器。
 *
 * 用法：
 *   ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xxx node scripts/gateway-check.cjs
 *   （可选 ZW_PROFILE 指定环境名，默认 gw-check）
 */
const path = require('node:path');

let chromium = null;
const candidates = [
  'playwright-core',
  path.join('/root/.nvm/versions/node/' + process.version + '/lib/node_modules/playwright-core'),
  path.join(process.env.HOME || '', '.nvm/versions/node/' + process.version + '/lib/node_modules/playwright-core'),
  '/usr/lib/node_modules/playwright-core',
  '/usr/local/lib/node_modules/playwright-core',
];
for (const p of candidates) {
  try {
    const mod = require(p);
    if (mod && mod.chromium && typeof mod.chromium.connectOverCDP === 'function') { chromium = mod.chromium; break; }
  } catch (_) { /* 继续试下一个 */ }
}
if (!chromium) {
  console.error('找不到 playwright-core（需要 connectOverCDP 可用的版本）。');
  for (const p of candidates) console.error('  - ' + p);
  process.exit(2);
}

const B = process.env.ZW_CORE || 'http://127.0.0.1:8730';
const T = process.env.ZW_ADMIN_TOKEN || '';
const NAME = process.env.ZW_PROFILE || 'gw-check';
const H = { Authorization: 'Bearer ' + T, 'Content-Type': 'application/json' };

const passed = [];
const failed = [];
const check = (name, cond, detail) => {
  (cond ? passed : failed).push(name);
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || !detail ? '' : ' — ' + detail}`);
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function j(method, path_, body) {
  const r = await fetch(B + path_, { method, headers: H, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch (_) { data = { raw: await r.text().catch(() => '') }; }
  return { status: r.status, data };
}

(async () => {
  console.log('== 准备环境 ==');
  await fetch(`${B}/api/v1/profiles/${NAME}?purge=true`, { method: 'DELETE', headers: H });
  let r = await j('POST', '/api/v1/profiles', { name: NAME, note: '网关自检', fp_timezone: 'Asia/Shanghai', fp_lang: 'zh-CN' });
  check('建环境', r.status === 200 && r.data.ok, JSON.stringify(r.data).slice(0, 200));
  const pid = r.data.data.id;

  r = await j('POST', `/api/v1/profiles/${pid}/start`, {});
  check('启动', r.status === 200 && r.data.ok, JSON.stringify(r.data).slice(0, 300));
  const cdp = r.data.data.cdp || {};
  check('返回 gateway_ws_url', !!cdp.gateway_ws_url, JSON.stringify(cdp).slice(0, 200));
  check('ws 地址里带令牌', String(cdp.gateway_ws_url).includes('token='));
  check('ws 地址指向网关而不是回环端口', !String(cdp.gateway_ws_url).includes(':' + cdp.local_port));

  console.log('== 发现接口地址重写 ==');
  const raw = await fetch(cdp.raw_cdp_json, { headers: H });
  const info = await raw.json();
  check('发现接口可达', raw.status === 200, String(raw.status));
  check('webSocketDebuggerUrl 已重写为网关地址', String(info.webSocketDebuggerUrl || '').startsWith('ws://'), info.webSocketDebuggerUrl);
  check('webSocketDebuggerUrl 里没有 127.0.0.1:本地端口', !String(info.webSocketDebuggerUrl || '').includes(':' + cdp.local_port));
  check('devtoolsFrontendUrl 不再是网关绝对地址', !String(info.devtoolsFrontendUrl || '').startsWith('http://'), info.devtoolsFrontendUrl);

  console.log('== Playwright connectOverCDP（走网关）==');
  const browser = await chromium.connectOverCDP(cdp.gateway_ws_url);
  check('connectOverCDP 成功', !!browser);
  const ctx = browser.contexts()[0] || (await browser.newContext());
  check('拿到 context', !!ctx);

  const page = await ctx.newPage();
  await page.goto('https://example.com', { waitUntil: 'domcontentloaded', timeout: 45000 });
  check('通过网关导航成功', page.url().includes('example.com'), page.url());
  const title = await page.title();
  check('能读到页面标题', title.includes('Example'), title);

  // 写一个 cookie，再通过 REST 读回来 —— 证明两条路径看的是同一个浏览器
  await ctx.addCookies([{ name: 'gw_marker', value: 'from-playwright', domain: '.example.com', path: '/', secure: true }]);
  r = await j('GET', `/api/v1/profiles/${pid}/cookies`);
  const got = (r.data.data.cookies || []).find(c => c.name === 'gw_marker');
  check('REST 能读到 Playwright 写的 cookie', !!got && got.value === 'from-playwright', JSON.stringify(got || {}).slice(0, 200));

  // 反向：REST 写入，Playwright 读
  await j('PUT', `/api/v1/profiles/${pid}/cookies`, {
    cookies: [{ name: 'rest_marker', value: 'from-rest', domain: '.example.org', path: '/', secure: true }],
    mode: 'merge',
  });
  const cookies2 = await ctx.cookies('https://example.org');
  check('Playwright 能读到 REST 写的 cookie', cookies2.some(c => c.name === 'rest_marker'), JSON.stringify(cookies2.map(c => c.name)));

  // 管理标签页不能劫持这一页
  const before = page.url();
  await j('POST', '/api/v1/page/navigate', { profile: pid, url: 'https://example.net' });
  await sleep(500);
  check('REST 导航没有劫持 Playwright 正在看的页面', page.url() === before, `${before} -> ${page.url()}`);

  console.log('== 令牌与并发边界 ==');
  const bad = await fetch(`${B}/cdp/${pid}/json/version?token=zwc_bogus`);
  check('错令牌被拒', bad.status === 401, String(bad.status));

  r = await j('GET', `/api/v1/profiles/${pid}/cdp?ttl=60`);
  check('可指定令牌有效期', r.data.data.token_ttl === 60, String(r.data.data.token_ttl));
  const tooShort = await j('GET', `/api/v1/profiles/${pid}/cdp?ttl=1`);
  check('过短的有效期被拒', tooShort.status === 422, String(tooShort.status));

  await browser.close();
  await sleep(300);

  // 断开后应该能再连（连接计数要正确回收）
  const again = await chromium.connectOverCDP(cdp.gateway_ws_url);
  check('断开后可以再次连接', !!again);
  await again.close();

  console.log('== 收拾 ==');
  await j('POST', `/api/v1/profiles/${pid}/stop`);
  await fetch(`${B}/api/v1/profiles/${pid}?purge=true`, { method: 'DELETE', headers: H });

  console.log();
  console.log(`通过 ${passed.length} 项，失败 ${failed.length} 项`);
  if (failed.length) { console.log('失败项：' + failed.join('、')); process.exit(1); }
  console.log('✅ 网关端到端自检全部通过');
})().catch(e => { console.error('❌ 异常：', e && e.message ? e.message : e); process.exit(1); });
