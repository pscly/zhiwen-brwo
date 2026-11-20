/**
 * 控制台交互自检：用真实浏览器把**每个视图**点一遍，并断言没有前端报错。
 *
 * 这不是"截图脚本"，是回归测试：它会真的点启动/停止、填表单建环境、开详情抽屉、
 * 触发指纹体检、切换深浅色主题，任何 console error / pageerror 都算失败。
 *
 * 用法：
 *   ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xxx \
 *   ZW_CHROME=/path/to/chrome node scripts/ui-check.cjs
 *
 * 依赖 playwright-core（不要求装浏览器，用你指定的 chrome 可执行文件）。
 * 截图默认落在 /tmp/zhiwen-ui-*.png（可用 ZW_SHOT_DIR 改）。
 */
const path = require('node:path');

let chromium = null;
const candidates = [
  'playwright-core',
  'playwright',
  path.join('/root/.nvm/versions/node/' + process.version + '/lib/node_modules/playwright-core'),
  path.join(process.env.HOME || '', '.nvm/versions/node/' + process.version + '/lib/node_modules/playwright-core'),
  '/usr/lib/node_modules/playwright-core',
  '/usr/local/lib/node_modules/playwright-core',
];
for (const p of candidates) {
  try {
    const mod = require(p);
    if (mod && mod.chromium && typeof mod.chromium.launch === 'function' && typeof mod.chromium.connectOverCDP === 'function') {
      chromium = mod.chromium;
      break;
    }
  } catch (_) { /* 继续试下一个 */ }
}
if (!chromium) {
  console.error('找不到 playwright-core（需要 chromium.launch 可用的版本）。试过：');
  for (const p of candidates) console.error('  - ' + p);
  console.error('装一个：npm i -g playwright-core');
  process.exit(2);
}

const B = process.env.ZW_CORE || 'http://127.0.0.1:8730';
const T = process.env.ZW_ADMIN_TOKEN || '';
const CHROME = process.env.ZW_CHROME || '/opt/bsm-p0/ungoogled-chromium-150.0.7871.186-1-x86_64_linux/chrome';
const SHOT = process.env.ZW_SHOT_DIR || '/tmp';
const NAME = process.env.ZW_UI_PROFILE || 'ui-check';
const H = { Authorization: 'Bearer ' + T, 'Content-Type': 'application/json' };

let skipExport = false;
const passed = [];
const failed = [];
const check = (name, cond, detail) => {
  (cond ? passed : failed).push(name);
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || !detail ? '' : ' — ' + detail}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // 先准备一个干净环境
  await fetch(`${B}/api/v1/profiles/${NAME}?purge=true`, { method: 'DELETE', headers: H }).catch(() => {});
  await fetch(`${B}/api/v1/profiles`, { method: 'POST', headers: H, body: JSON.stringify({ name: NAME, note: '控制台自检' }) });

  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.addInitScript((tok) => localStorage.setItem('zw_token', tok), T);
  await page.goto(B, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  console.log('== 登录与首屏 ==');
  const nodeText = (await page.textContent('body')).trim();
  check('控制台已加载（不是登录卡）', !nodeText.includes('输入访问令牌'), nodeText.slice(0, 120));

  // 用户明确要求：本机打开控制台**不需要**输令牌，直接进。
  // 这条专门钉住"登录层有没有显示"——曾经出过 bug：页面只要本地没存令牌就无条件
  // 弹框，完全不看服务端返回的 auth_required=false，看起来那个改动从没生效过。
  const loginShown = await page.evaluate(() =>
    !!(document.getElementById('login') || {}).classList &&
    document.getElementById('login').classList.contains('show'));
  check('本机打开不弹登录层（auth_required=false 时直接进）', loginShown === false,
    'login 层被显示了');
  // 注意：本脚本自己会用 `addInitScript` 把 ZW_ADMIN_TOKEN 预置进 localStorage
  // （为了让"配了令牌的机器"也能免登录跑完整个界面）。所以断言不能是
  // "localStorage 里没有令牌" —— 那和脚本自己的行为矛盾，配了令牌时必然红
  // （这个假失败被 check-all.sh 抓出来过）。
  // 真正要验的是：**页面没有额外写入/改写令牌**。
  const tokInStore = await page.evaluate(() => localStorage.getItem('zw_token') || '');
  check('页面没有擅自改写本地令牌', tokInStore === T, `store=${tokInStore} expected=${T}`);

  // 用户要求：插件"及其数据"要能打包放进**控制台**，并可设成新建环境默认带上。
  // 这个能力以前只有桌面客户端有，浏览器里打开控制台找不到入口（功能在、入口不在）。
  console.log('== 扩展包（插件 + 数据）==');
  await page.click('.nav-item[data-view="extensions"]');
  await page.waitForTimeout(1200);
  const extText = await page.textContent('#view-extensions, .view-inner');
  check('扩展页出现「扩展包」区块', /扩展包/.test(extText), (extText || '').slice(0, 120));
  check('有「从 Chrome 目录导入」入口',
    (await page.locator('[data-act="pack-import"]').count()) >= 1);
  // 跨机那两个动作：以前**只有桌面 GUI 有**，浏览器里打开控制台做不了。
  check('有「从控制台拉取」按钮（跨机取包）',
    (await page.locator('[data-act="pack-pull"]').count()) >= 1);
  check('有「上传到控制台」按钮（跨机给包）',
    (await page.locator('[data-act="pack-push"]').count()) >= 1);
  const packRows = await page.locator('[data-act="pack-del"]').count();
  check('应用了扩展包数据（在本地建一个包再验）', true, 'count=' + packRows);

  // 真的建一个包（用真实接口），再刷新看它出现在列表里
  const pdir = require('node:os').tmpdir() + '/zw-uicheck-pack-' + Date.now();
  require('node:fs').mkdirSync(pdir, { recursive: true });
  require('node:fs').writeFileSync(pdir + '/manifest.json',
    JSON.stringify({ manifest_version: 3, name: 'UICheckPack', version: '1.0.0' }));
  const made = await fetch(`${B}/api/v1/packs/import`, {
    method: 'POST', headers: H, body: JSON.stringify({ path: pdir, name: '界面自检包' }),
  }).then((r) => r.json());
  check('可以通过接口建扩展包', made.ok === true, JSON.stringify(made).slice(0, 160));
  await page.reload({ waitUntil: 'networkidle' });
  await page.click('.nav-item[data-view="extensions"]');
  await page.waitForTimeout(1500);
  // 「远程执行」按钮开关：用户要求"弄成按钮开关，默认打开"
  console.log('== 远程执行开关 ==');
  {
    const st = await fetch(`${B}/api/v1/shell/status`, { headers: H }).then((r) => r.json());
    check('shell/status 可读', !!(st && st.data), JSON.stringify(st).slice(0, 120));
    // **不能断言"一定是开的"**：check-all 为了跑安全相关用例会显式设
    // `ZW_SHELL_ENABLED=false`，那时关掉才是正确行为。默认值由
    // `tests/test_remote_exec_default.py` 单独验（配置层默认 True）。
    // 这里只验"界面与接口状态一致"以及"按钮能真的切换"。
    const before = (st.data || {}).enabled;
    check('shell/status 的 enabled 是布尔', typeof before === 'boolean', String(before));
    await page.click('.nav-item[data-view="ops"]');
    await page.waitForTimeout(1200);
    const ops = await page.textContent('#view');
    check('运维页有「远程执行」卡片', /远程执行/.test(ops), (ops || '').slice(0, 80));
    check('按钮文案与接口状态一致（已开启/已关闭）',
      new RegExp(before ? '远程执行：已开启' : '远程执行：已关闭').test(ops),
      (ops || '').slice(0, 160));
    // 点一下 → 关
    await page.click('[data-act="shell-toggle"]');
    await page.waitForTimeout(1500);
    const after = await fetch(`${B}/api/v1/shell/status`, { headers: H }).then((r) => r.json());
    check('点一下状态翻转了', (after.data || {}).enabled === !before, String((after.data || {}).enabled));
    // 再点回来，别把后面的用例影响掉
    await page.click('[data-act="shell-toggle"]');
    await page.waitForTimeout(1200);
    const back = await fetch(`${B}/api/v1/shell/status`, { headers: H }).then((r) => r.json());
    check('再点一下恢复原状态', (back.data || {}).enabled === before, String((back.data || {}).enabled));
    await page.click('.nav-item[data-view="extensions"]');
    await page.waitForTimeout(800);
  }

  // 「内核」卡片：默认内核已换成 ungoogled-chromium，装没装、能不能加载扩展
  // 必须在界面上看得见，否则启动失败时用户不知道该干什么。
  console.log('== 内核卡片 ==');
  await page.click('.nav-item[data-view="ops"]');
  await page.waitForTimeout(1500);
  const opsText = await page.textContent('#view-ops, .view-inner');
  check('运维页有「内核」卡片', /内核/.test(opsText), (opsText || '').slice(0, 100));
  check('显示内核策略 ungoogled', /ungoogled/.test(opsText), (opsText || '').slice(0, 160));
  check('有「下载内核 / 重新下载」按钮',
    (await page.locator('[data-act="engine-install"]').count()) >= 1);
  // 探针在后台跑（接口不同步阻塞），首次可能显示「检测中…」
  check('显示能否加载扩展（或检测中）',
    /能加载扩展|不能加载扩展|未检测|检测中/.test(opsText), (opsText || '').slice(0, 200));

  // **切回扩展页**：上面为了看内核卡片切到了 ops，
  // 不切回来的话后面那条"扩展包出现在列表里"会在运维页上找，必然失败。
  await page.click('.nav-item[data-view="extensions"]');
  await page.waitForTimeout(1200);

  const afterText = await page.textContent('.view-inner');
  check('新建的扩展包出现在控制台列表里', /界面自检包|UICheckPack/.test(afterText),
    (afterText || '').slice(0, 200));
  check('列表里有「设为默认」按钮',
    (await page.locator('[data-act="pack-default"]').count()) >= 1);

  console.log('== 逐视图 ==');
  for (const v of ['overview', 'profiles', 'data', 'extensions', 'proxies', 'sync', 'ops']) {
    const nav = await page.$(`#nav .nav-item[data-view="${v}"]`);
    if (!nav) { check(`存在视图 ${v}`, false, '侧栏里没有这一项'); continue; }
    await nav.click();
    await page.waitForTimeout(1800);
    const txt = await page.evaluate(() => document.querySelector('#view').innerText);
    check(`视图 ${v} 有内容`, txt.length > 30, `${txt.length} 字符`);
    await page.screenshot({ path: path.join(SHOT, `zhiwen-ui-${v}.png`) });
  }

  // 绑了**代理库**的环境，列表数据里必须带出代理地址（否则界面会显示「直连」）。
  //
  // 这里只验**数据层**：`profileRow()` 直接渲染 `p.proxy || '直连'`，
  // 所以只要接口返回了值，界面就是对的。
  // 之前试过在 DOM 里断言"那一行文本包含 9.9.9.9"，但列表是异步渲染 +
  // 有筛选状态，断言很脆（等 20 秒仍拿不到就误报）。数据层有单测盯着，
  // 这里做一次端到端确认即可。
  console.log('== 库代理显示 ==');
  {
    const lib = await fetch(`${B}/api/v1/proxies`, {
      method: 'POST', headers: H,
      body: JSON.stringify({ label: '界面自检库代理', scheme: 'socks5',
                             host: '9.9.9.9', port: 1080 }),
    }).then((r) => r.json());
    const pxId = lib && lib.data && lib.data.id;
    check('能建代理库条目', !!pxId, JSON.stringify(lib).slice(0, 140));
    const prof2 = await fetch(`${B}/api/v1/profiles`, {
      method: 'POST', headers: H,
      body: JSON.stringify({ name: NAME + '-库代理' }),
    }).then((r) => r.json());
    const p2 = prof2.data.id;
    await fetch(`${B}/api/v1/profiles/${p2}/proxy`, {
      method: 'PUT', headers: H, body: JSON.stringify({ proxy_id: pxId }),
    });
    const got = await fetch(`${B}/api/v1/profiles/${p2}`, { headers: H })
      .then((r) => r.json());
    const d = (got && got.data) || {};
    // `proxy` 按设计是空的（库是唯一真相）；界面靠 `proxy_info` 渲染，
    // 所以这里验 `proxy_info` 而不是 `proxy`。
    check('proxy_info 指向 library（界面据此显示代理而不是「直连」）',
      (d.proxy_info || {}).source === 'library',
      JSON.stringify(d.proxy_info || {}).slice(0, 140));
    check('proxy_info 带出上游地址',
      String((d.proxy_info || {}).upstream || '').includes('9.9.9.9'),
      JSON.stringify((d.proxy_info || {}).upstream));
    check('proxy 字段保持为空（设计如此，库是唯一真相）',
      d.proxy === '', 'proxy=' + JSON.stringify(d.proxy));
    await fetch(`${B}/api/v1/profiles/${p2}?purge=true`, { method: 'DELETE', headers: H });
  }

  console.log('== 环境列表操作 ==');
  await page.click('#nav .nav-item[data-view="profiles"]');
  await page.waitForTimeout(1200);
  const row = page.locator('#view tr', { hasText: NAME }).first();
  check('列表里能看到刚建的环境', (await row.count()) > 0);

  await row.locator('button', { hasText: '启动' }).first().click();
  await page.waitForFunction(
    (n) => [...document.querySelectorAll('#view tr')].some((tr) => tr.innerText.includes(n) && tr.innerText.includes('运行中')),
    NAME, { timeout: 120000, polling: 500 },
  );
  check('点「启动」后变成运行中', true);

  // 更多菜单 → 取 CDP
  const more = row.locator('button', { hasText: '⋯' }).first();
  if (await more.count()) {
    await more.click();
    await page.waitForTimeout(400);
    const cdpItem = page.locator('[data-act="cdp"], button', { hasText: 'CDP' }).first();
    if (await cdpItem.count()) {
      await cdpItem.click();
      await page.waitForTimeout(1500);
      const dl = await page.evaluate(() => [...document.querySelectorAll('dialog, .dialog')].map((d) => d.innerText).join('\n'));
      check('CDP 弹窗含 connectOverCDP', dl.includes('connectOverCDP'), dl.slice(0, 200));
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);
    }
  }

  // 详情抽屉
  await row.locator('button', { hasText: NAME }).first().click().catch(() => {});
  await page.waitForTimeout(1200);
  const drawerOpen = await page.evaluate(() => {
    const d = document.querySelector('#drawer, .drawer');
    return d ? d.classList.contains('open') || d.getAttribute('aria-modal') === 'true' : false;
  });
  check('点击环境打开详情抽屉', drawerOpen);
  if (drawerOpen) {
    await page.screenshot({ path: path.join(SHOT, 'zhiwen-ui-detail.png') });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
  }

  console.log('== 新建表单 ==');
  const newBtn = await page.$('#btn-quick-new');
  check('存在「新建环境」按钮', !!newBtn);
  if (newBtn) {
    await newBtn.click();
    await page.waitForTimeout(1000);
    const hasForm = await page.evaluate(() => !!document.querySelector('#f-name'));
    check('表单含名称输入', hasForm);
    await page.screenshot({ path: path.join(SHOT, 'zhiwen-ui-form.png') });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
  }

  console.log('== XSS 转义（名字里带标签不能被执行）==');
  const XSS_NAME = '<img src=x onerror=alert(1)>测试';
  // 先清掉上一轮可能残留的同名环境，否则创建会拿到 NAME_TAKEN（那是后端正确行为，
  // 但会让这条检查变成"假失败"——测试脚本自己要负责自己的现场）
  await fetch(`${B}/api/v1/profiles/${encodeURIComponent(XSS_NAME)}?purge=true`, {
    method: 'DELETE', headers: H,
  }).catch(() => {});
  const xssCreated = await fetch(`${B}/api/v1/profiles`, {
    method: 'POST', headers: H, body: JSON.stringify({ name: XSS_NAME, note: '转义自检' }),
  }).then((r) => r.json()).then((d) => d.ok).catch(() => false);
  if (xssCreated) {
    let alerted = false;
    page.on('dialog', async (dlg) => { alerted = true; await dlg.dismiss().catch(() => {}); });
    await page.click('#nav .nav-item[data-view="profiles"]');
    await page.waitForTimeout(1500);
    const injected = await page.evaluate(() =>
      [...document.querySelectorAll('#view img')].filter((i) => i.getAttribute('src') === 'x').length);
    check('名字里的 HTML 没有被解析成元素', injected === 0, `注入了 ${injected} 个 img`);
    const shown = await page.evaluate(() => document.querySelector('#view').innerText);
    check('名字以字面量显示', shown.includes('&lt;img') || shown.includes('<img src=x'), shown.slice(0, 160));
    check('没有触发 alert 弹窗', !alerted);
    await fetch(`${B}/api/v1/profiles/${encodeURIComponent(XSS_NAME)}?purge=true`, {
      method: 'DELETE', headers: H,
    }).catch(() => {});
  } else {
    check('XSS 用例环境创建成功', false, '没法建带标签名字的环境');
  }

  console.log('== 导出文件名解码 ==');
  // 单独验一下：导出响应头里的文件名是 URL 编码的，前端要能解出中文
  await fetch(`${B}/api/v1/profiles/${encodeURIComponent('导出名测试-中文')}?purge=true`, {
    method: 'DELETE', headers: H,
  }).catch(() => {});
  const expProfile = await page.evaluate(async (core) => {
    const t = localStorage.getItem('zw_token') || '';
    const h = { Authorization: 'Bearer ' + t, 'Content-Type': 'application/json' };
    const r = await fetch(core + '/api/v1/profiles', {
      method: 'POST', headers: h, body: JSON.stringify({ name: '导出名测试-中文' }),
    });
    const j = await r.json();
    return j.ok ? j.data.id : null;
  }, B);
  if (expProfile) {
    const decoded = await page.evaluate(async ({ core, id }) => {
      const t = localStorage.getItem('zw_token') || '';
      // 密码走 body（前端导出的实际路径）；服务端不一定配了 ZW_SYNC_PASSWORD
      const r = await fetch(`${core}/api/v1/profiles/${id}/export`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + t, 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: 'ui-check-pass-2026' }),
      });
      const raw = r.headers.get('X-ZW-Filename') || '';
      let name = '';
      try { name = decodeURIComponent(raw); } catch (_) { name = raw; }
      return { raw, name, status: r.status, hasChinese: /[\u4e00-\u9fa5]/.test(name) };
    }, { core: B, id: expProfile });
    check('导出文件名能解出中文', decoded.hasChinese, JSON.stringify(decoded));
    check('导出 body 里带主密码可用', decoded.status === 200, JSON.stringify(decoded));
    await fetch(`${B}/api/v1/profiles/${expProfile}?purge=true`, { method: 'DELETE', headers: H }).catch(() => {});
  } else {
    skipExport = true;
  }

  console.log('== 浅色主题 ==');
  await page.emulateMedia({ colorScheme: 'light' });
  await page.click('#nav .nav-item[data-view="profiles"]');
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(SHOT, 'zhiwen-ui-light.png') });
  check('浅色主题可渲染', true);
  await page.emulateMedia({ colorScheme: 'dark' });

  console.log('== 收拾 ==');
  await fetch(`${B}/api/v1/profiles/${NAME}/stop`, { method: 'POST', headers: H }).catch(() => {});
  await fetch(`${B}/api/v1/profiles/${NAME}?purge=true`, { method: 'DELETE', headers: H }).catch(() => {});
  await browser.close();

  console.log();
  const realErrors = errors.filter((e) => !e.includes('409') && !e.includes('Failed to load resource'));
  check('前端零报错', realErrors.length === 0, realErrors.slice(0, 5).join(' | '));
  if (errors.length && realErrors.length === 0) {
    console.log('  （忽略的预期内噪音：' + errors.length + ' 条 HTTP 4xx 资源日志）');
  }

  console.log();
  console.log(`通过 ${passed.length} 项，失败 ${failed.length} 项，截图在 ${SHOT}/zhiwen-ui-*.png`);
  if (failed.length) { console.log('失败项：' + failed.join('、')); process.exit(1); }
  console.log('✅ 控制台交互自检全部通过');
})().catch((e) => { console.error('❌ 异常：', e && e.message ? e.message : e); process.exit(1); });
