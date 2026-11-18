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
const http = require('node:http');

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

// 等到某个环境真的处于 running（负载高时 start 返回 ok、但进程可能还在起）。
// 偶发失败排查起来最费时间：0.7.58 那轮 gateway-check 在 check-all 里红过一次、
// 单独跑 20/20 全过 —— 就是这条"启动后立刻读 cookie"的时序问题。
async function waitRunning(pid, timeoutMs = 30000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const st = await j('GET', `/api/v1/profiles/${pid}`);
    const d = (st.data && st.data.data) || {};
    if (d.state === 'running') return d;
    await sleep(300);
  }
  return null;
}

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

  // **导航用本地页面，不依赖公网**。
  //
  // 真踩过（2026-11-19）：这里原来访问 https://example.com，某次 check-all 里
  // 那条 CDP 连接挂了 **643 秒**才断 —— 期间没有任何日志，后面所有 cookie 断言
  // 全部失败。单跑时网络好就过、跑全套时偶发红，看起来像网关/cookie 代码坏了，
  // 实际是"验收步骤依赖了外网"。
  //
  // 验收要证明的是"经网关的 CDP 能开页面、能读 DOM"，用本地 HTTP 页足够，
  // 而且结果确定。外网连通性另有专门的检测（browserleaks/ip123），不在这里做。
  const lite = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    // 注意：JS **不会**把相邻的两个字符串字面量自动拼接（那是 Python 的行为），
    // 必须显式加 `+`，否则整个文件都是语法错误（missing ) after argument list）。
    res.end('<!doctype html><html><head><title>GW Check Page</title></head>'
            + '<body><h1 id="gw-h1">hello gateway</h1></body></html>');
  });
  await new Promise((r) => lite.listen(0, '127.0.0.1', r));
  const litePort = lite.address().port;
  try {
    await page.goto(`http://127.0.0.1:${litePort}/`, { waitUntil: 'domcontentloaded', timeout: 45000 });
    check('通过网关导航成功', page.url().includes(`:${litePort}`), page.url());
    // `title()` 也可能挂住（导航后渲染卡死时），给它单独的超时上限
    const title = await Promise.race([
      page.title(),
      new Promise((r) => setTimeout(() => r(''), 15000)),
    ]);
    check('能读到页面标题', title.includes('GW Check Page'), title);
    const h1 = await Promise.race([
      page.textContent('#gw-h1'),
      new Promise((r) => setTimeout(() => r(''), 15000)),
    ]);
    check('能读到页面 DOM 内容', (h1 || '').includes('hello gateway'), h1);
  } finally {
    lite.close();
  }

  // 等它真的 running 再读 cookie：否则会拿到 STATE_CONFLICT，
  // 而 `r.data.data` 是 undefined → 报 "Cannot read properties of undefined (reading 'cookies')"
  const running = await waitRunning(pid);
  check('环境确实进入 running', !!running, running ? '' : '等了 30 秒仍未 running');

  // 写一个 cookie，再通过 REST 读回来 —— 证明两条路径看的是同一个浏览器
  await ctx.addCookies([{ name: 'gw_marker', value: 'from-playwright', domain: '.example.com', path: '/', secure: true }]);
  // 读 cookie 要有耐心：浏览器冷启动时 CDP 的 `Network.enable` 可能几十秒才回，
  // 服务端已在 dump_cookies 里做了一次重连兜底，这里再给几次机会并**打印真实错误**。
  // 以前这里直接 `r.data.data.cookies`，服务端一旦 500 就只看到
  // "Cannot read properties of undefined (reading 'cookies')" —— 看不出真正原因。
  let cookieList = null;
  let lastErr = '';
  for (let i = 0; i < 5; i++) {
    r = await j('GET', `/api/v1/profiles/${pid}/cookies`);
    const d = r.data && r.data.data;
    if (d && Array.isArray(d.cookies)) { cookieList = d.cookies; break; }
    lastErr = JSON.stringify(r.data || {}).slice(0, 240);
    await new Promise((res) => setTimeout(res, 1500));
  }
  check('REST 能读到 cookie 列表', cookieList !== null,
    lastErr || '连续 5 次都没拿到 cookies 字段');
  const got = (cookieList || []).find(c => c.name === 'gw_marker');
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
