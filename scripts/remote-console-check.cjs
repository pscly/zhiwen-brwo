/**
 * 远程控制台自检：**从非回环地址**打开客户端控制台，验证"凭机器码进入"。
 *
 * 为什么单独一个脚本：`ui-check.cjs` 走的是 127.0.0.1（本机免令牌那条路），
 * 而用户的需求里还有一半是"如果远程需要访问客户端的控制台，只需要输入客户端的
 * 机器码即可"。这两条路完全不同：
 *
 *   回环   → auth_required=false，直接进
 *   非回环 → 401，必须给机器码或 ZW_ADMIN_TOKEN
 *
 * 只在真浏览器里点一遍才算验过 —— API 层能过不代表登录框认机器码。
 *
 * 用法：
 *   ZW_CORE_IP=192.168.3.6 ZW_CORE=http://127.0.0.1:8894 \
 *   ZW_DEVICE_ID=zwd_xxx node scripts/remote-console-check.cjs
 */
const { chromium } = require('/root/.nvm/versions/node/v24.22.1/lib/node_modules/playwright-core');

const LAN_IP = process.env.ZW_CORE_IP || '';
const LOOPBACK = (process.env.ZW_CORE || 'http://127.0.0.1:8730').replace(/\/$/, '');
const DEV = process.env.ZW_DEVICE_ID || '';
const CHROME = process.env.ZW_CHROME
  || '/opt/bsm-p0/ungoogled-chromium-150.0.7871.186-1-x86_64_linux/chrome';

let pass = 0, fail = 0;
const check = (name, ok, detail) => {
  if (ok) { pass++; console.log('  ✅ ' + name); }
  else { fail++; console.log('  ❌ ' + name + (detail ? '  — ' + detail : '')); }
};

(async () => {
  if (!LAN_IP || !DEV) {
    console.error('需要 ZW_CORE_IP 与 ZW_DEVICE_ID');
    process.exit(2);
  }
  const port = new URL(LOOPBACK).port;
  const remoteBase = `http://${LAN_IP}:${port}`;

  console.log('== 回环：本机免令牌 ==');
  const r1 = await fetch(`${LOOPBACK}/api/v1/meta`);
  const m1 = await r1.json();
  check('回环 meta 可读', r1.status === 200 && !!m1.data, String(r1.status));
  check('回环 auth_required=false', m1.data && m1.data.auth_required === false,
    JSON.stringify(m1.data && m1.data.auth_required));
  check('确实监听在 0.0.0.0', m1.data && m1.data.host === '0.0.0.0', m1.data && m1.data.host);

  console.log('== 非回环：必须凭机器码 ==');
  const r2 = await fetch(`${remoteBase}/api/v1/meta`);
  const m2 = await r2.json().catch(() => ({}));
  check('非回环被拒（401）', r2.status === 401, String(r2.status));
  check('提示里提到机器码', /机器码/.test(JSON.stringify(m2)), JSON.stringify(m2).slice(0, 120));

  const r3 = await fetch(`${remoteBase}/api/v1/meta`, {
    headers: { Authorization: `Bearer ${DEV}` },
  });
  check('带机器码后可读', r3.status === 200, String(r3.status));

  console.log('== 真浏览器：登录框认机器码 ==');
  const b = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message));
  await p.goto(remoteBase, { waitUntil: 'networkidle', timeout: 45000 });
  await p.waitForTimeout(2500);
  const body1 = await p.textContent('body');
  check('远程打开要求登录', /机器码|访问令牌/.test(body1));
  check('登录文案提到「机器码」', /机器码/.test(body1));

  const input = p.locator('#login input, input[type="text"], input[type="password"]').first();
  check('有输入框', (await input.count()) > 0);
  await input.fill(DEV);
  const btn = p.locator('#login button, button:has-text("进入"), button:has-text("连接")').first();
  await btn.click();
  await p.waitForTimeout(4000);
  const body2 = await p.textContent('body');
  // 判据要精确：登录层是 `#login.show`，进入后应隐藏。
  // 不要用"页面里出现'机器码'三个字"来否定 —— 控制台的说明文字里本来就有。
  const loginVisible = await p.evaluate(() => {
    const el = document.getElementById('login');
    return !!(el && el.classList.contains('show'));
  });
  check('登录层已隐藏（真的进去了）', loginVisible === false);
  check('控制台主界面已渲染（有总览/环境导航）',
    /总览/.test(body2) && /环境/.test(body2),
    body2.replace(/\s+/g, ' ').slice(0, 80));
  check('控制台无 JS 报错', errs.length === 0, errs.slice(0, 2).join('; '));
  await b.close();

  console.log(`\n通过 ${pass} 项，失败 ${fail} 项`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('异常：', e.message); process.exit(1); });
