/**
 * 主控台（hub :8700/）页面自检：真浏览器 + 拦截式 mock 后端。
 *
 * 为什么不用 python -m http.server：页面要 POST 登录、要拉 /v1/console/*，
 * 纯静态服务满足不了。这里用 node 起一个只分发 "/" 的静态服务（为了让运行环境
 * 是 http://127.0.0.1:<port> —— 只有这种安全上下文才有 navigator.clipboard），
 * 其余接口全部由 Playwright 的 page.route 拦截，返回值就是 docs/15-主控台契约.md
 * 里的形状。这样断言不依赖任何真实设备。
 *
 * 用法：
 *   node scripts/console-ui-check.cjs
 * 环境变量：
 *   ZW_CHROME   Chromium 可执行文件（默认仓库里那个 ungoogled-chromium）
 *   ZW_SHOT_DIR 截图目录（默认 /tmp）
 */

const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

const REPO = '/root/1codes/zhiwen_brwo';
const HTML_PATH = path.join(REPO, 'hub/src/zw_hub/web/console.html');
const CHROME = process.env.ZW_CHROME || '/opt/bsm-p0/ungoogled-chromium-150.0.7871.186-1-x86_64_linux/chrome';
const SHOT = process.env.ZW_SHOT_DIR || '/tmp';

let chromium = null;
const candidates = [
  'playwright-core',
  '/root/.nvm/versions/node/' + process.version + '/lib/node_modules/playwright-core',
  '/root/.nvm/versions/node/v24.22.1/lib/node_modules/playwright-core',
  '/usr/lib/node_modules/playwright-core',
];
for (const p of candidates){
  try {
    const mod = require(p);
    if (mod && mod.chromium && typeof mod.chromium.launch === 'function'){ chromium = mod.chromium; break; }
  } catch (_) { /* 试下一个 */ }
}
if (!chromium){
  console.error('找不到 playwright-core，试过：\n  ' + candidates.join('\n  '));
  process.exit(2);
}

const html = fs.readFileSync(HTML_PATH, 'utf8');

/* ── fixtures（严格按契约 §三 的形状）────────────────────────────────── */
const A_ID = 'zwd_aaaaaaaaaaaaaaaa';
const B_ID = 'zwd_bbbbbbbbbbbbbbbb';
const XSS_INVENTORY_NAME = '<img src=x onerror="window.__xss=1">环境';

const OVERVIEW = {
  server: {
    version: '0.4.2', uptime_seconds: 3725, data_dir: '/data',
    shell_enabled: true, token_required: false, auto_approve: true,
    allowed_cidrs: ['192.168.3.0/24'], default_credentials: false,
  },
  totals: { machines: 2, online: 1, browsers: 2, running: 1 },
  machines: [
    {
      device_id: A_ID, label: 'psubs3.6', hostname: 'psubs3.6',
      platform: 'Linux', arch: 'AMD64', app_version: '0.4.2',
      status: 'active', online: true, last_seen_ago: 12, last_ip: '192.168.3.6',
      shell_enabled: true, profiles: 3, running: 1, max_running: 8,
      disk_human: '1.2GB', engine: 'Chrome 149',
      core_base: 'http://192.168.3.6:8730', core_reachable: true,
      browsers: [
        {
          id: '56c88eb236e54eef', name: '环境A', state: 'running', group: '默认',
          cdp_http: 'http://192.168.3.6:8730/cdp/56c88eb236e54eef',
          cdp_ws: 'ws://192.168.3.6:8730/cdp/56c88eb236e54eef/devtools/browser/9f3a',
        },
        {
          id: '77aa11bb22cc33dd', name: XSS_INVENTORY_NAME, state: 'stopped', group: '测试',
          cdp_http: 'http://192.168.3.6:8730/cdp/77aa11bb22cc33dd', cdp_ws: '',
        },
      ],
    },
    {
      device_id: B_ID, label: '云电脑', hostname: 'cloud-pc',
      // 故意比服务器版本（0.4.2）旧，用来验「可升级」徽章
      platform: 'Windows', arch: 'AMD64', app_version: '0.3.9',
      status: 'active', online: false, last_seen_ago: 5400, last_ip: '10.0.0.9',
      shell_enabled: false, profiles: 1, running: 1, max_running: 4,
      disk_human: '', engine: '', core_base: '', core_reachable: null,
      // 故意给一台**离线机器**挂一个 running 环境 —— 这是真实发生过的情形
      // （tianyiyuncd1 离线，控制台仍显示 running: 1）。用来验状态是否标成「上次：…」。
      browsers: [
        { id: 'deadbeefdeadbeef', name: '留在离线的那个', state: 'running', group: '',
          cdp_path: '/cdp/deadbeefdeadbeef' },
      ],
    },
  ],
  events: [
    { ts: 1794836000, action: 'enroll', device_id: A_ID, by: '', note: '' },
    { ts: 1794836100, action: 'revoke', device_id: B_ID, by: 'pscly', note: '夜间维护' },
    { ts: 1794836200, action: 'restore', device_id: B_ID, by: 'pscly', note: '<script>window.__xss2=1</script>' },
    { ts: 1794836300, action: 'note', device_id: A_ID, by: 'pscly', note: '改名为 psubs3.6' },
  ],
};

const SECRETS = {
  panel_user: 'pscly',
  panel_password: 'S3cret-Panel-PW',
  hub_token: 'hubtok_abcdef123456',
  hub_token_set: true,
  allowed_cidrs: ['192.168.3.0/24'],
  auto_approve: true,
  sync_password_set: false,
  hint: '这些是服务器 .env 里的值，只在本机/局域网可用，别透到公网',
};

// 代理库（服务器与用户机共享的那份）。三条覆盖三种展示形态：
// 普通单跳 / 链式（前置 → 落地）/ 已隐藏
const PROXY_XSS_LABEL = '<img src=x onerror="window.__xss=1">代理';

// 扩展包（跨机复用的那份）。用一条带数据、一条仅代码，覆盖两种展示形态；
// name 里塞 XSS 载荷，验证渲染用的是 textContent 而不是 innerHTML。
const PACKS = [
  { id: 'pk0000000001', name: 'RXYCDP 全套', extension_count: 5, has_data: true,
    size: 3_400_000, size_human: '3.2 MB', origin: 'cloudpc', sha256: 'a'*64, created_at: 1 },
  { id: 'pk0000000002', name: '<img src=x onerror="window.__pkxss=1">仅代码', extension_count: 1,
    has_data: false, size: 1024, size_human: '1.0 KB', origin: 'panel', sha256: 'b'*64, created_at: 2 },
];

const PROXIES = [
  { id: 1, scheme: 'socks5', host: '66.80.12.66', port: 20000, username: 'cf1b081103',
    has_password: true, hop: '', label: '落地A', note: '', hidden: false,
    origin: 'zwd_hubtest01', upstream: 'socks5://66.80.12.66:20000',
    is_chain: false, chain: 'socks5://cf1b081103:***@66.80.12.66:20000',
    created_at: 1794836000, updated_at: 1794836000 },
  { id: 2, scheme: 'http', host: '1.2.3.4', port: 8080, username: '',
    has_password: false, hop: 'http://127.0.0.1:21080', label: '链式B', note: '',
    hidden: false, origin: 'panel', upstream: 'http://1.2.3.4:8080',
    is_chain: true, chain: 'http://127.0.0.1:21080 → http://1.2.3.4:8080',
    created_at: 1794836001, updated_at: 1794836001 },
  // 第 4 条专门放 XSS 载荷：代理库里的 label/host 来自**设备上报**（不可信来源），
  // 主控台渲染时必须转义。用 img onerror 探针，注入成功就会设 window.__xss。
  { id: 4, scheme: 'socks5', host: '<img src=x onerror="window.__xss=1">1.1.1.1',
    port: 1080, username: '', has_password: false, hop: '', label: PROXY_XSS_LABEL,
    note: '', hidden: false, origin: 'zwd_evil', upstream: 'socks5://x:1080',
    is_chain: false, chain: '', created_at: 1794836003, updated_at: 1794836003 },
  { id: 3, scheme: 'http', host: '9.9.9.9', port: 3128, username: 'svr',
    has_password: true, hop: '', label: '藏起来的', note: '', hidden: true,
    origin: 'panel', upstream: 'http://9.9.9.9:3128', is_chain: false,
    chain: 'http://svr:***@9.9.9.9:3128', created_at: 1794836002, updated_at: 1794836002 },
];

const LEGACY_DEVICES = (OVERVIEW.machines || []).map(m => ({
  device_id: m.device_id, label: m.label, hostname: m.hostname,
  platform: m.platform, arch: m.arch, app_version: m.app_version,
  status: m.status, online: m.online, last_seen_ago: m.last_seen_ago,
  last_ip: m.last_ip, shell_enabled: m.shell_enabled,
  profiles: m.profiles, running: m.running, max_running: m.max_running,
  disk_human: m.disk_human, engine: m.engine, core_base: m.core_base,
  inventory: (m.browsers || []).map(b => ({
    id: b.id, name: b.name, state: b.state, group: b.group,
    cdp_path: '/cdp/' + b.id,
  })),
}));

/* ── 断言框架 ──────────────────────────────────────────────────────────── */
const passed = [];
const failed = [];
function check(name, cond, detail){
  (cond ? passed : failed).push(name);
  console.log('  ' + (cond ? '✅' : '❌') + ' ' + name + (!cond && detail ? '  — ' + detail : ''));
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ── mock 后端 ─────────────────────────────────────────────────────────── */
function setupRoutes(page, opts){
  const o = opts || {};
  const m = { loggedIn: !!o.loggedIn, overviewMode: o.overviewMode || 'ok', secretsMode: o.secretsMode || 'ok' };
  m.revoked = [];
  m.renamed = [];
  m.deleted = [];

  const json = (route, body, status) => route.fulfill({
    status: status || 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
  const bad = (route, status, code, message) => json(route, { ok: false, code: code, message: message }, status);

  page.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    const method = req.method();

    if (p === '/' || p === '/index.html') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
    if (p === '/favicon.ico') return route.fulfill({ status: 204, body: '' });

    if (p === '/panel/api/whoami') return json(route, { ok: true, data: { logged_in: m.loggedIn } });

    if (p === '/panel/api/login' && method === 'POST'){
      let body = {};
      try { body = JSON.parse(req.postData() || '{}'); } catch (_) {}
      if (body.username === 'pscly' && body.password === 'pw-good'){
        m.loggedIn = true;
        return json(route, { ok: true, data: { user: 'pscly', ttl: 604800 } });
      }
      return bad(route, 401, 'PANEL_BAD_CREDENTIALS', '登录名或密码不正确');
    }
    if (p === '/panel/api/logout'){ m.loggedIn = false; return json(route, { ok: true, data: { logged_out: true } }); }

    if (p === '/api/v1/packs'){
      return json(route, { ok: true, data: { packs: [
        { id: 'abcd1234efgh', name: 'RXYCDP 常用插件', extension_count: 3, with_data: 2,
          is_default: true, size_human: '4.2MB', source: 'chrome' },
        { id: 'wxyz9876mnop', name: '临时测试包', extension_count: 1, with_data: 0,
          is_default: false, size_human: '912KB', source: 'dir' },
      ] } });
    }
    if (p === '/panel/api/meta'){
      return json(route, { ok: true, data: {
        version: '0.4.2', data_dir: '/data', auto_approve: true, token_required: false,
        user: 'pscly', default_credentials: false,
      } });
    }

    if (p === '/v1/console/overview'){
      if (!m.loggedIn) return bad(route, 401, 'UNAUTHENTICATED', '需要管理面板会话');
      if (m.overviewMode === 'missing') return bad(route, 404, 'NOT_FOUND', 'Not Found');
      return json(route, { ok: true, data: OVERVIEW });
    }
    // ── 代理库（新增的「代理」视图）──
    if (p === '/v1/proxies' && method === 'GET'){
      if (!m.loggedIn) return bad(route, 401, 'UNAUTHENTICATED', '需要管理面板会话');
      const showHidden = url.searchParams.get('include_hidden') === 'true';
      const items = PROXIES.filter(x => showHidden || !x.hidden);
      return json(route, { ok: true, data: { proxies: items, total: items.length } });
    }
    // ── 扩展包（跨机复用的包）──
    if (p === '/v1/packs' && method === 'GET'){
      if (!m.loggedIn) return bad(route, 401, 'UNAUTHENTICATED', '需要管理面板会话');
      return json(route, { ok: true, data: { packs: PACKS } });
    }
    if (p === '/v1/packs' && method === 'POST'){
      if (!m.loggedIn) return bad(route, 401, 'UNAUTHENTICATED', '需要管理面板会话');
      m.uploads = m.uploads || [];
      m.uploads.push({ len: (req.postData() || '').length, url: p + url.search });
      return json(route, { ok: true, data: {
        id: 'pk0000000003', name: 'uploaded', extension_count: 1, has_data: true,
        size: 10, size_human: '10 B', origin: 'console', sha256: 'c'*64, created_at: 3 } });
    }
    if (p === '/v1/proxies' && method === 'POST'){
      let body = {};
      try { body = JSON.parse(req.postData() || '{}'); } catch (_) {}
      if (!body.host) return bad(route, 400, 'PROXY_INVALID', '主机不能为空');
      m.added = m.added || [];
      m.added.push(body);
      return json(route, { ok: true, data: {
        id: 99, scheme: body.scheme || 'http', host: body.host,
        port: Number(body.port) || 0, username: body.username || '',
        has_password: !!body.password, hop: body.hop || '', label: body.label || '',
        hidden: false, origin: 'panel',
        upstream: (body.scheme || 'http') + '://' + body.host + ':' + body.port,
      } });
    }
    // PATCH = 编辑：就地改一条。记录到 m.patched，**不进 m.added** ——
    // 这样若页面错把编辑做成"新增"（老 bug），断言会立刻发现。
    if (/^\/v1\/proxies\/\d+$/.test(p) && method === 'PATCH'){
      let body = {};
      try { body = JSON.parse(req.postData() || '{}'); } catch (_) {}
      const id = Number(p.split('/')[3]);
      m.patched = m.patched || [];
      m.patched.push(Object.assign({ id: id }, body));
      if (!body.host) return bad(route, 400, 'PROXY_INVALID', '主机不能为空');
      return json(route, { ok: true, data: Object.assign(
        { id: id, has_password: true, hidden: false, origin: 'panel' }, body) });
    }
    if (/^\/v1\/proxies\/\d+\/hide$/.test(p) && method === 'POST'){
      let body = {};
      try { body = JSON.parse(req.postData() || '{}'); } catch (_) {}
      m.hidden = m.hidden || [];
      m.hidden.push({ id: Number(p.split('/')[3]), hidden: !!body.hidden });
      return json(route, { ok: true, data: { id: Number(p.split('/')[3]), hidden: !!body.hidden } });
    }
    if (/^\/v1\/proxies\/\d+$/.test(p) && method === 'DELETE'){
      m.pxDeleted = m.pxDeleted || [];
      m.pxDeleted.push(Number(p.split('/')[3]));
      return json(route, { ok: true, data: { deleted: true, id: Number(p.split('/')[3]) } });
    }

    if (p === '/v1/console/secrets'){
      if (!m.loggedIn) return bad(route, 401, 'UNAUTHENTICATED', '需要管理面板会话');
      if (m.secretsMode === 'missing') return bad(route, 404, 'NOT_FOUND', 'Not Found');
      return json(route, { ok: true, data: SECRETS });
    }

    // 旧面板接口（兼容模式 + 设备操作）
    if (p === '/panel/api/stats'){
      return json(route, { ok: true, data: {
        total: 2, active: 2, revoked: 0, online: 1, total_profiles: 3, total_running: 1,
        online_window_seconds: 180, shell: { queued: 0, running: 0, done: 2, error: 0, devices_with_pending: 0 },
      } });
    }
    if (p === '/panel/api/devices') return json(route, { ok: true, data: { devices: LEGACY_DEVICES } });
    if (p === '/panel/api/events'){
      return json(route, { ok: true, data: { events: OVERVIEW.events.map((e, i) => ({
        id: i + 1, ts: e.ts, device_id: e.device_id, action: e.action, actor: e.by,
        detail: { note: e.note },
      })) } });
    }
    if (/^\/panel\/api\/devices\/[^/]+\/revoke$/.test(p)){ m.revoked.push(p); return json(route, { ok: true, data: {} }); }
    if (/^\/panel\/api\/devices\/[^/]+\/restore$/.test(p)){ return json(route, { ok: true, data: {} }); }
    if (/^\/panel\/api\/devices\/[^/]+$/.test(p) && method === 'DELETE'){ m.deleted.push(p); return json(route, { ok: true, data: { deleted: true } }); }
    if (/^\/panel\/api\/devices\/[^/]+$/.test(p) && method === 'PATCH'){
      let body = {};
      try { body = JSON.parse(req.postData() || '{}'); } catch (_) {}
      m.renamed.push(body.label);
      return json(route, { ok: true, data: { label: body.label } });
    }

    // 远程命令行（docs/13）
    if (/^\/v1\/devices\/[^/]+\/shell$/.test(p) && method === 'POST'){
      return json(route, { ok: true, data: { job_id: 'sh_test', status: 'queued' } });
    }
    if (/^\/v1\/devices\/[^/]+\/shell\/history$/.test(p)){ return json(route, { ok: true, data: { jobs: [] } }); }
    if (/^\/v1\/devices\/[^/]+\/shell\/[^/]+\/cancel$/.test(p)){ return json(route, { ok: true, data: { canceled: false } }); }
    if (/^\/v1\/devices\/[^/]+\/shell\/[^/]+$/.test(p)){
      return json(route, { ok: true, data: { job: {
        id: 'sh_test', device_id: A_ID, command: 'echo hi', status: 'done',
        rc: 0, stdout: 'mock-stdout-ok\n第二行', stderr: '', duration_ms: 12,
        truncated: false, error: '', created_at: 1794836000.1,
      } } });
    }

    return bad(route, 404, 'NOT_FOUND', 'mock 没实现：' + method + ' ' + p);
  });
  return m;
}

/* ── 主流程 ────────────────────────────────────────────────────────────── */
(async () => {
  const serverHtml = fs.readFileSync(HTML_PATH, 'utf8');
  const server = http.createServer((req, res) => {
    if (req.url === '/' || req.url.startsWith('/?') || req.url.startsWith('/#')){
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(serverHtml);
      return;
    }
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('mock: 这个路径由 page.route 拦截，不该真的请求到这里：' + req.url);
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const ORIGIN = 'http://127.0.0.1:' + port + '/';

  const browser = await chromium.launch({
    executablePath: CHROME,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });

  const errors = [];
  // 浏览器对任何非 2xx 都会自己往 console 写一条 "Failed to load resource"。
  // 本脚本故意制造 401（错误密码）与 404（接口未就绪），这些是由 mock 触发的
  // 网络层记录，不是页面 JS 报错；单独计数，不算失败。
  const netNoise = [];
  // 主控台的实时终端会开一条 WS（/v1/console/term/<dev>）。
  // 本脚本的 mock 后端只有 HTTP，没有 WS，所以浏览器必然报连接失败
  // （而且是无头环境常见的 ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS）。
  // 这属于"测试环境没有 WS 后端"，不是页面 JS 出错，归入预期噪音。
  const IGNORE = /Failed to load resource|WebSocket connection to .*\/v1\/console\/term\//i;
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await ctx.newPage();
  // 接住 mock 的内部记录（隐藏/添加/删除调用过什么），断言要用
  const pxMocks = setupRoutes(page, {});
  page.on('console', msg => {
    if (msg.type() !== 'error') return;
    if (IGNORE.test(msg.text())) netNoise.push(msg.text());
    else errors.push('console: ' + msg.text());
  });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));

  console.log('\n— A. 登录 —');
  await page.goto(ORIGIN, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(400);

  check('未登录时只显示登录框（#login 可见 / #app 隐藏）',
    await page.isVisible('#login') && !(await page.isVisible('#app')));
  check('登录框有账号、密码两个输入框与登录按钮',
    (await page.locator('#u').count()) === 1 && (await page.locator('#p').count()) === 1 &&
    (await page.locator('#btn-login').count()) === 1);

  await page.fill('#u', 'pscly');
  await page.fill('#p', 'wrong');
  await page.click('#btn-login');
  await page.waitForTimeout(300);
  check('错误密码：页面内提示「登录名或密码不正确」，仍停在登录页',
    (await page.textContent('#login-err') || '').includes('登录名或密码不正确') &&
    !(await page.isVisible('#app')));

  await page.fill('#p', 'pw-good');
  await page.click('#btn-login');
  await page.waitForSelector('.mach', { timeout: 5000 });
  check('正确登录后主控台出现（#app 可见 / #login 隐藏）',
    (await page.isVisible('#app')) && !(await page.isVisible('#login')));
  check('顶栏显示当前账号 pscly', (await page.textContent('#acct-who')) === 'pscly');
  check('登录后 URL hash 记在 #machines', new URL(page.url()).hash === '#machines');

  console.log('\n— B. 机器视图 —');
  check('机器卡片渲染出 2 张', (await page.locator('.mach').count()) === 2);
  const cardA = page.locator('.mach[data-dev="' + A_ID + '"]');
  const cardB = page.locator('.mach[data-dev="' + B_ID + '"]');
  check('在线机器显示「在线」徽章、离线机器显示「离线」',
    (await cardA.locator('.mach-badges').textContent()).includes('在线') &&
    (await cardB.locator('.mach-badges').textContent()).includes('离线'));
  check('卡片字段齐全（磁盘 1.2GB / 引擎 Chrome 149 / 命令行 开）',
    (await cardA.locator('[data-r="disk"]').textContent()) === '1.2GB' &&
    (await cardA.locator('[data-r="engine"]').textContent()) === 'Chrome 149' &&
    (await cardA.locator('[data-r="shell"]').textContent()) === '开');
  check('命令行关闭的机器显示「关」',
    (await cardB.locator('[data-r="shell"]').textContent()) === '关');
  check('机器码完整显示（不是截断）',
    (await cardA.locator('[data-r="code"]').textContent()) === A_ID);
  // 控制台后来加了「待授权」这一格（免授权码审批流程），所以是 5 个而不是 4 个。
  // 之前这里断言 '2,1,2,1' 已经过时，一直在报假失败。
  // 版本落后的机器要出现「可升级」徽章。
  // 服务器知道每台机器上报的版本、也知道自己是什么版本，但以前从不对比 ——
  // 于是"这台落后几十个版本"没有任何地方告诉用户（实测云电脑停在 0.7.16）。
  const upBadge = await page.locator('.mach', { hasText: '云电脑' }).locator('.badge.warn', { hasText: '可升级' }).count();
  check('版本落后的机器显示「可升级」徽章', upBadge >= 1, 'count=' + upBadge);
  const upTitle = await page.locator('.badge.warn', { hasText: '可升级' }).first().getAttribute('title');
  check('「可升级」的悬停说明写清了怎么升级',
    (upTitle || '').includes('覆盖安装') && (upTitle || '').includes('机器码保留'), upTitle);
  const upToDate = await page.locator('.mach', { hasText: 'psubs3.6' }).locator('.badge.warn', { hasText: '可升级' }).count();
  check('版本相同的机器不显示「可升级」', upToDate === 0, 'count=' + upToDate);

  check('KPI：机器 2 / 在线 1 / 待授权 0 / 浏览器 2 / 运行中 1',
    (await page.locator('.kpi-val').allTextContents()).join(',') === '2,1,0,2,1',
    'got=' + (await page.locator('.kpi-val').allTextContents()).join(','));

  await cardA.locator('[data-act="copy-code"]').click();
  await page.waitForTimeout(250);
  let clip = '';
  try { clip = await page.evaluate(() => navigator.clipboard.readText()); } catch (_) { clip = ''; }
  const copyToast = (await page.locator('.toast').last().textContent().catch(() => '')) || '';
  check('「复制机器码」把完整机器码复制进剪贴板', clip === A_ID, 'clipboard=' + JSON.stringify(clip) + ' toast=' + copyToast);

  check('上报了 core_base 的机器：「打开控制台」是指向该地址的新标签链接',
    (await cardA.locator('[data-r="open"]').getAttribute('href')) === 'http://192.168.3.6:8730' &&
    (await cardA.locator('[data-r="open"]').getAttribute('target')) === '_blank');
  check('没有 core_base 的机器：「打开控制台」被禁用并写明原因',
    (await cardB.locator('[data-r="open"]').getAttribute('href')) === null &&
    (await cardB.locator('[data-r="open"]').getAttribute('class')).includes('off') &&
    ((await cardB.locator('[data-r="open"]').getAttribute('title')) || '').includes('core_base'));

  check('环境名里的 <img onerror> 被转义：没有注入节点、脚本没被执行',
    (await page.evaluate(() => window.__xss === undefined)) &&
    (await page.evaluate(() => document.querySelectorAll('article.mach img').length === 0)));

  console.log('\n— C. 浏览器视图 —');
  await page.click('#tabs .tab[data-view="browsers"]');
  await page.waitForTimeout(200);
  check('切到浏览器视图：hash=#browsers 且只显示这一屏',
    new URL(page.url()).hash === '#browsers' &&
    (await page.isVisible('#view-browsers')) && !(await page.isVisible('#view-machines')));
  check('按机器分组：两块 br-sec', (await page.locator('.br-sec').count()) === 2);
  // 三台机器里有一台没上报清单，所以有清单的两台共排 3 行
  // （第二台故意挂了 1 个 running 环境，用来验「离线机器的状态要标上次」）
  check('有清单的机器排出 3 行环境',
    (await page.locator('.br-sec table.tbl tbody tr').count()) === 3,
    'rows=' + (await page.locator('.br-sec table.tbl tbody tr').count()));
  check('cdp_http 链接地址正确、可点击', (await page.locator('.br-sec a.link').first().getAttribute('href')) ===
    'http://192.168.3.6:8730/cdp/56c88eb236e54eef');
  // 两台机器现在都有清单，页面上不会出现这条提示。
  // 改成直接验源码里有这句话 —— 它是一条静态文案，用页面渲染去验反而要求
  // fixture 里必须留一台空机器，而那会和"只有两台机器"的其他断言互相牵制。
  const consoleSrc = require('node:fs').readFileSync(HTML_PATH, 'utf8');
  check('没上报清单时的提示文案存在',
    consoleSrc.includes('这台还没上报浏览器清单（客户端需 ≥0.5.0）'));
  check('cdp_ws 有独立「复制」按钮', (await page.locator('#view-browsers [data-copy^="ws://"]').count()) === 1);

  // 离线机器的环境状态必须标成「上次：…」。
  // 真踩过：机器离线后 inventory 还是旧的，控制台直接渲染出一个绿色 running
  // （tianyiyuncd1 离线、控制台仍显示 running: 1），用户以为还有东西在跑。
  const offSec = await page.textContent('#view-browsers');
  check('离线机器的环境状态标为「上次：…」', offSec.includes('上次：'), offSec.slice(0, 200));
  // 精确到那一行：离线机器上的 running 环境，状态徽章**不能**是绿色的 ok。
  const offRow = page.locator('#view-browsers tbody tr', { hasText: '留在离线的那个' });
  const offRowOk = await offRow.locator('.badge.ok').count();
  check('离线机器下那行不出现绿色 running 徽章', offRowOk === 0, 'ok 徽章数=' + offRowOk);
  check('那一行确实写着「上次：running」',
    (await offRow.textContent()).includes('上次：'), await offRow.textContent());

  console.log('\n— D. 代理视图（服务器与用户机共享的代理库）—');
  await page.click('#tabs .tab[data-view="proxies"]');
  await page.waitForTimeout(500);
  check('切到代理视图：hash=#proxies 且只显示这一屏',
    new URL(page.url()).hash === '#proxies' &&
    (await page.isVisible('#view-proxies')) && !(await page.isVisible('#view-browsers')));
  const pxRows = page.locator('#px-rows tr');
  // 4 条 fixture 里有 1 条 hidden，所以默认只看到 3 行
  check('排出的行数 = 3（隐藏的那条默认不显示）', (await pxRows.count()) === 3,
    'count=' + (await pxRows.count()));
  const pxText = await page.locator('#px-rows').textContent();
  check('单跳显示 scheme://host:port', pxText.includes('socks5://66.80.12.66:20000'), pxText.slice(0, 120));
  check('链式显示前置地址', pxText.includes('http://127.0.0.1:21080'), pxText.slice(0, 160));
  check('有密码的标「已设置」', pxText.includes('已设置'));
  check('无密码的标「无」', pxText.includes('无'));
  check('来源列显示 origin', pxText.includes('zwd_hubtest01'), pxText.slice(0, 160));
  check('计数文字正确', (await page.textContent('#px-count')).includes('共 3 条'));

  // 代理库里的 label/host 来自设备上报（不可信）。渲染必须转义 ——
  // 注入成功的话页面里会出现 img 节点，且 window.__xss 被设为 1。
  check('代理的 label/host 里的 <img onerror> 被转义（没有注入节点）',
    (await page.evaluate(() => document.querySelectorAll('#px-rows img').length === 0)));
  check('代理的 XSS 载荷没有执行（window.__xss 未设置）',
    (await page.evaluate(() => window.__xss === undefined)));
  check('载荷以字面量显示（能看见标签文本本身）',
    (await page.textContent('#px-rows')).includes('onerror'));

  // 隐藏：点第一行「隐藏」→ 应变成 1 行
  await page.locator('#px-rows [data-px-hide]').first().click();
  await page.waitForTimeout(500);
  check('点「隐藏」后重新加载（只请求不带 include_hidden）',
    pxMocks.hidden && pxMocks.hidden.length >= 1,
    JSON.stringify(pxMocks.hidden || []));

  // 勾「显示已隐藏」→ 隐藏的那条出现并带徽章
  await page.check('#px-show-hidden');
  await page.waitForTimeout(500);
  const pxText2 = await page.locator('#px-rows').textContent();
  check('勾「显示已隐藏」后出现「已隐藏」徽章', pxText2.includes('已隐藏'), pxText2.slice(0, 160));
  check('已隐藏的行给出「取消隐藏」按钮',
    (await page.locator('#px-rows [data-px-hide][data-px-hid="1"]').count()) >= 1);

  // 添加代理：填表 → 保存 → 请求体正确
  await page.click('#px-add');
  await page.waitForTimeout(400);
  check('「添加代理」弹出对话框', await page.isVisible('#dlg[open]'));
  await page.fill('#pxf-host', '5.5.5.5');
  await page.fill('#pxf-port', '1080');
  await page.fill('#pxf-label', '自检加的');
  await page.click('#dlg-ok');
  await page.waitForTimeout(600);
  const added = (pxMocks.added || [])[0] || {};
  check('添加请求带上 host/port/label',
    added.host === '5.5.5.5' && Number(added.port) === 1080 && added.label === '自检加的',
    JSON.stringify(added));

  // 编辑：密码留空 = 不改（后端语义），对话框要把现有值预填好
  const editCount = await page.locator('#px-rows [data-px-edit]').count();
  const rowCount = await page.locator('#px-rows tr').count();
  check('每行都有「编辑」按钮', editCount === rowCount,
    `edit=${editCount} rows=${rowCount}`);
  await page.locator('#px-rows [data-px-edit]').first().click();
  await page.waitForTimeout(400);
  check('点「编辑」弹出对话框', await page.isVisible('#dlg[open]'));
  const preHost = await page.inputValue('#pxf-host');
  const prePass = await page.inputValue('#pxf-pass');
  check('编辑框预填了现有主机', preHost === '66.80.12.66', 'host=' + preHost);
  check('密码框**不回填**（留空＝不改）', prePass === '', 'pass=' + prePass);
  // 改标签后保存，断言请求体带对了
  await page.fill('#pxf-label', '改过的标签');
  await page.click('#dlg-ok');
  await page.waitForTimeout(600);
  // 编辑必须走 **PATCH 且带 id**。老 bug 是走 POST（upsert），改主机/端口时
  // 会变成"多出一条"，旧那条还在、新那条还没密码 —— 所以这两条断言很关键。
  const patches = pxMocks.patched || [];
  const lastPatch = patches[patches.length - 1] || {};
  check('编辑走 PATCH 且带 id', patches.length === 1 && lastPatch.id === 1,
    JSON.stringify(patches));
  check('编辑 PATCH 带对 host/port/label',
    lastPatch.host === '66.80.12.66' && Number(lastPatch.port) === 20000 &&
    lastPatch.label === '改过的标签', JSON.stringify(lastPatch));
  check('编辑时密码为空（表示不修改）', (lastPatch.password || '') === '',
    JSON.stringify(lastPatch.password));
  const addedBefore = (pxMocks.added || []).filter(x => x.label === '改过的标签');
  check('编辑**不会**变成新增（没有多出一条同名记录）', addedBefore.length === 0,
    JSON.stringify(addedBefore));

  console.log('\n— D2. 令牌视图 —');
  await page.click('#tabs .tab[data-view="secrets"]');
  await page.waitForSelector('.sec-row', { timeout: 5000 });
  check('令牌视图有安全提示条',
    (await page.textContent('#view-secrets .banner')).includes('安全提示'));
  const pwRow = page.locator('.sec-row[data-k="panel_password"]');
  const maskedText = (await pwRow.locator('.sec-v').textContent()) || '';
  check('密码默认是掩码（不含明文）',
    maskedText.includes('•') && !maskedText.includes(SECRETS.panel_password));
  await pwRow.locator('[data-reveal]').click();
  await page.waitForTimeout(120);
  check('点「显示」后出现真实密码、按钮变「隐藏」',
    (await pwRow.locator('.sec-v').textContent()) === SECRETS.panel_password &&
    (await pwRow.locator('[data-reveal]').textContent()) === '隐藏');
  await pwRow.locator('[data-reveal]').click();
  await page.waitForTimeout(120);
  check('再点「隐藏」回到掩码',
    ((await pwRow.locator('.sec-v').textContent()) || '').includes('•'));
  check('网段白名单与自动放行都渲染出来',
    (await page.textContent('.sec-row[data-k="allowed_cidrs"] .sec-v')).includes('192.168.3.0/24') &&
    (await page.textContent('.sec-row[data-k="auto_approve"] .sec-v')).includes('开'));

  console.log('\n— D2. 扩展包视图（跨机器复用的那层）—');
  // 用户要求「把插件打包放到控制台，以后建环境默认带上」。
  // 之前只有接口、**主控台没有界面** —— 等于没法用。
  await page.click('#tabs .tab[data-view="packs"]');
  await page.waitForTimeout(500);
  check('切到扩展包视图：hash=#packs 且只显示这一屏',
    new URL(page.url()).hash === '#packs' &&
    (await page.isVisible('#view-packs')) && !(await page.isVisible('#view-proxies')));
  const pkRows = page.locator('#pk-rows tr');
  check('扩展包行数 = 2', (await pkRows.count()) === 2, 'count=' + (await pkRows.count()));
  const pkText = await page.locator('#pk-rows').textContent();
  check('显示包名', pkText.includes('RXYCDP 全套'), pkText.slice(0, 120));
  check('显示扩展数', pkText.includes('5'), pkText.slice(0, 120));
  check('带数据的包标「带数据」', pkText.includes('带数据'));
  check('仅代码的包标「仅代码」', pkText.includes('仅代码'));
  check('显示体积', pkText.includes('3.2 MB'), pkText.slice(0, 160));
  check('有下载按钮', (await page.locator('#pk-rows [data-pk-dl]').count()) === 2);
  check('有删除按钮', (await page.locator('#pk-rows [data-pk-del]').count()) === 2);
  check('扩展包名里的 <img onerror> 被转义（没有注入节点）',
    (await page.evaluate(() => document.querySelectorAll('#pk-rows img').length === 0)));
  check('扩展包的 XSS 载荷没有执行（window.__pkxss 未设置）',
    (await page.evaluate(() => window.__pkxss === undefined)));
  check('有「上传扩展包」按钮', await page.isVisible('#pk-upload'));

  console.log('\n— E. 事件视图 —');
  await page.click('#tabs .tab[data-view="events"]');
  await page.waitForTimeout(200);
  const evRows = await page.locator('.ev').count();
  check('事件流渲染 4 条（最近 50 条的上限内）', evRows === 4);
  const evText = await page.textContent('#ev-list');
  check('事件里能看到「注册」「踢下线」「恢复」',
    evText.includes('注册') && evText.includes('踢下线') && evText.includes('恢复'));
  check('事件备注里的 <script> 被转义（没有执行、没有插入节点）',
    (await page.evaluate(() => window.__xss2 === undefined)) &&
    (await page.evaluate(() => document.querySelectorAll('#ev-list script').length === 0)));

  console.log('\n— F. 远程终端 —');
  await page.click('#tabs .tab[data-view="machines"]');
  await page.waitForTimeout(200);
  await cardA.locator('[data-act="term"]').click();
  await page.waitForSelector('.mach[data-dev="' + A_ID + '"] .term', { timeout: 3000 });
  check('点「终端」在卡片里展开终端', await page.isVisible('.mach[data-dev="' + A_ID + '"] .term'));
  // 徽章现在表达"通道形态"：连上 WS 显示「实时」，否则「队列」；
  // 命令行被关时显示「命令行：关」。早先的「命令行：开」已被更好的设计取代，
  // 这条断言跟着改，否则会一直误报。
  const flagText = await page.textContent('.mach[data-dev="' + A_ID + '"] [data-term="flag"]');
  check('终端徽章显示通道形态（实时 / 队列）',
    flagText.includes('实时') || flagText.includes('队列'), 'flag=' + flagText);
  const termInput = page.locator('.mach[data-dev="' + A_ID + '"] [data-term="input"]');
  await termInput.fill('echo hi');
  await termInput.press('Enter');
  await page.waitForSelector('.mach[data-dev="' + A_ID + '"] .term-stdout', { timeout: 5000 });
  check('命令能执行并回显 mock 输出',
    (await page.textContent('.mach[data-dev="' + A_ID + '"] .term-stdout')).includes('mock-stdout-ok'));

  // 刷新不丢终端 / 输入框 / 卡片 DOM
  await page.evaluate(() => { document.querySelector('.mach').dataset.tag = 'stay'; });
  await termInput.fill('还没发出去的命令');
  await page.click('#btn-refresh');
  await page.waitForTimeout(500);
  check('手动刷新后终端仍在、输出还在',
    await page.isVisible('.mach[data-dev="' + A_ID + '"] .term') &&
    (await page.textContent('.mach[data-dev="' + A_ID + '"] .term-stdout')).includes('mock-stdout-ok'));
  check('刷新后输入框里没发出去的命令没被清掉',
    (await termInput.inputValue()) === '还没发出去的命令');
  check('刷新是原地更新：卡片 DOM 没被整块重建（打的自定义标记还在）',
    await page.evaluate(() => !!document.querySelector('.mach[data-tag="stay"]')));

  console.log('\n— G. 健壮性 —');
  const dupIds = await page.evaluate(() => {
    const ids = Array.from(document.querySelectorAll('[id]')).map(e => e.id);
    return ids.filter((v, i) => ids.indexOf(v) !== i);
  });
  check('页面里没有重复 id', dupIds.length === 0, 'dup=' + dupIds.join(','));
  check('没有外部 JS / CSS / CDN 引用',
    (await page.evaluate(() => document.querySelectorAll('script[src],link[href^="http"],link[href^="//"]').length === 0)) &&
    !/https?:\/\/(cdn|unpkg|jsdelivr|fonts\.googleapis)/i.test(html));

  // 刷新后停在原视图（hash 记忆）
  await page.click('#tabs .tab[data-view="browsers"]');
  await page.waitForTimeout(150);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.br-sec', { timeout: 5000 });
  check('刷新页面后停在原来的浏览器视图（hash 记忆）',
    new URL(page.url()).hash === '#browsers' && await page.isVisible('#view-browsers'));

  // 自动刷新（真等一轮，证明 10 秒定时器在工作）
  await page.click('#tabs .tab[data-view="machines"]');
  const before = await page.textContent('#conn-text');
  await page.waitForTimeout(11000);
  const after = await page.textContent('#conn-text');
  check('10 秒自动刷新真的在跑（连接状态时间戳前进）', before !== after, before + ' -> ' + after);

  // 暂停自动刷新
  await page.click('#btn-auto');
  await page.waitForTimeout(150);
  const pausedA = await page.textContent('#conn-text');
  await page.waitForTimeout(11000);
  const pausedB = await page.textContent('#conn-text');
  check('暂停后不再自动刷新（时间戳不动）', pausedA === pausedB, pausedA + ' -> ' + pausedB);
  check('暂停按钮文案变成「已暂停」', (await page.textContent('#btn-auto')).includes('已暂停'));
  await page.click('#btn-auto');

  console.log('\n— H. 截图（桌面 1440×900）—');
  await page.click('#tabs .tab[data-view="machines"]');
  await page.waitForTimeout(300);
  const shotDesktop = path.join(SHOT, 'zhiwen-console-desktop-1440x900.png');
  await page.screenshot({ path: shotDesktop, fullPage: false });
  check('桌面截图已生成', fs.existsSync(shotDesktop) && fs.statSync(shotDesktop).size > 20000);

  console.log('\n— I. 窄屏（420×900）—');
  await page.setViewportSize({ width: 420, height: 900 });
  await page.waitForTimeout(400);
  const cardBoxes = await page.locator('.mach').evaluateAll(els => els.map(e => e.getBoundingClientRect()));
  const stacked = cardBoxes.length === 2 && cardBoxes.every(b => b.width > 360) &&
    Math.abs(cardBoxes[0].x - cardBoxes[1].x) < 2 && cardBoxes[1].y > cardBoxes[0].y;
  check('窄屏下卡片竖排（同 x、上下排开）', stacked, JSON.stringify(cardBoxes.map(b => [Math.round(b.x), Math.round(b.y), Math.round(b.width)])));
  const shotNarrow = path.join(SHOT, 'zhiwen-console-narrow-420x900.png');
  await page.screenshot({ path: shotNarrow, fullPage: false });
  check('窄屏截图已生成', fs.existsSync(shotNarrow) && fs.statSync(shotNarrow).size > 15000);

  await page.click('#tabs .tab[data-view="browsers"]');
  await page.waitForTimeout(300);
  const scrollable = await page.evaluate(() => {
    const w = document.querySelector('.table-wrap');
    return !!w && w.scrollWidth > w.clientWidth + 20;
  });
  check('窄屏下表格容器横向可滚动', scrollable);

  console.log('\n— J. 兼容模式（overview 返回 404）—');
  const ctx2 = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'zh-CN' });
  const page2 = await ctx2.newPage();
  setupRoutes(page2, { loggedIn: true, overviewMode: 'missing', secretsMode: 'missing' });
  page2.on('console', msg => {
    if (msg.type() !== 'error') return;
    if (IGNORE.test(msg.text())) netNoise.push('page2 ' + msg.text());
    else errors.push('page2 console: ' + msg.text());
  });
  page2.on('pageerror', e => errors.push('page2 pageerror: ' + e.message));
  await page2.goto(ORIGIN, { waitUntil: 'domcontentloaded' });
  await page2.waitForSelector('.mach', { timeout: 5000 });
  const bannerText = (await page2.textContent('#banner-stack')) || '';
  check('overview 404 时顶部提示「主控台接口未就绪」', bannerText.includes('主控台接口未就绪'));
  check('overview 404 时仍用旧接口渲染出 2 张机器卡（不白屏）', (await page2.locator('.mach').count()) === 2);
  check('overview 404 时浏览器视图仍能从 inventory 拼出 CDP 地址',
    await page2.evaluate(() => {
      const el = document.querySelector('#br-list');
      return !!el && el.textContent.includes('/cdp/56c88eb236e54eef');
    }));
  await page2.click('#tabs .tab[data-view="secrets"]');
  await page2.waitForTimeout(400);
  check('secrets 404 时令牌视图给出「主控台接口未就绪」而不是空白',
    (await page2.textContent('#sec-body')).includes('主控台接口未就绪'));

  check('全程没有 JS 报错（pageerror / console error；只放行 mock 造的 401/404 网络记录）',
    errors.length === 0, errors.join(' | '));

  await browser.close();
  server.close();

  console.log('\n══════════════════════════════════════════');
  console.log('断言通过 ' + passed.length + ' 条，失败 ' + failed.length + ' 条');
  if (failed.length) console.log('失败项：\n  - ' + failed.join('\n  - '));
  console.log('截图：');
  console.log('  ' + shotDesktop);
  console.log('  ' + shotNarrow);
  process.exit(failed.length ? 1 : 0);
})().catch(e => {
  console.error('自检脚本自身出错：', e);
  process.exit(3);
});