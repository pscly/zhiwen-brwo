# AGENTS.md · 给 AI 的项目工作手册

> 你正在 **zhiwen_brwo** 仓库里工作。这份文件是给 AI 代理（Claude / Codex / Cursor / OpenCode 等）读的，
> 目的是让你**不用读完全部文档就能安全地改这个项目**。

---

## 一、这是什么

知纹（zhiwen）= 指纹浏览器机群管理器。

```
core/    zw-core：每台电脑一个。管环境、开浏览器、给局域网提供 CDP、和 hub 同步
hub/     zw-hub：一台就够（已部署在 192.168.3.5:8700）。存加密快照 + 跨机租约
docs/    全中文文档
scripts/ 部署与自检脚本
tests/   601 项单元测试（含真起 hub 的同步集成测试）
```

**它不实现指纹**。指纹由 `fingerprint-chromium`（`--fingerprint=<种子>`）或系统 Chrome 提供。

---

## 二、开工前必读（三处注释，别跳）

| 文件 | 为什么必须读 |
|---|---|
| `core/src/zw_core/services/cookies.py` 顶部 | 为什么 CDP 命令必须挂 **页面会话**（对浏览器级端点发 `Network.enable` 会 `-32601`） |
| `core/src/zw_core/gateway.py` 顶部 | 为什么网关的核心职责是**重写地址**而不是转发 |
| `core/src/zw_core/services/bundle.py` 顶部 | 为什么跨机**不能**搬 `Default/Cookies`（DPAPI，且 Chromium 会把解不开的 cookie 当损坏删掉） |

改这三处之前，先在回复里说明你理解了上面的原因。

还有两处**也**必须先读，它们是 0.2.0 踩出来的新坑：

| 文件 | 为什么必须读 |
|---|---|
| `core/src/zw_core/api.py` 顶部 | **路由顺序陷阱**：`/profiles/batch`、`/profiles/import`、`/sync/push-all` 必须声明在 `/profiles/{ref}`、`/sync/{ref}/*` 之前，否则会被当成"环境名叫 batch" |
| `core/src/zw_core/services/engines.py` 的 `wait_ready` | **端口归属校验**：只信 `/json/version` 会在端口被别的 chrome 占住时把别人的 ws 登记成自己的路由（串号） |

---

## 三、硬约束（改了就是 bug）

1. **浏览器永远只监听 127.0.0.1**。对外只经网关。
2. **同一环境同一时刻只允许一个持有者**。本地租约 + hub 租约，两道。
3. **密钥指纹不一致必须在落盘前中止**。顺序不能调。
4. **落盘必须原子**（staging → rename），绝不原地覆盖。
5. **上传用 CAS**（`If-None-Match: *`），绝不静默覆盖别人的版本。
6. **`fingerprint_seed` 不可直接改**，只能走 `fingerprint/reset`。
7. **任何日志/响应里不得出现 cookie 值、密码、令牌原文**。
8. **所有面向用户的文字用中文**（错误消息、日志消息、文档、UI）。代码标识符保持英文。
9. **不许让 uvicorn/反代替我们信 `X-Forwarded-For`**。"来自回环 = 免令牌"是鉴权基础，
   默认 `proxy_headers=False`；要开 `ZW_TRUST_PROXY_HEADERS` 时，反代必须**覆盖**
   该头（`proxy_set_header X-Forwarded-For $remote_addr`），不能追加。

---

## 四、跑起来

```bash
# 依赖
uv venv .venv && uv pip install --python .venv/bin/python -e ./core -e ./hub

# 测试（必须全绿才算改对）。pytest 是可选依赖，clone 后要先装：
#   uv pip install --python .venv/bin/python -e "./core[dev]" pytest-asyncio
.venv/bin/python -m pytest tests -q

# 开发服务（pidfile 管理，不要用 pkill 免得误杀自己的 shell）
bash scripts/run-dev.sh
bash scripts/stop-dev.sh

# 端到端（单机，真开浏览器；服务需先跑起来）
ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xxx ZW_SYNC_PASSWORD=xxx \
  .venv/bin/python scripts/e2e-check.py
```

环境变量见 `docs/03-部署指南.md` §4。开发用的样例在 `.env.test`（不入 git）。

---

## 五、改代码的规矩

| 场景 | 做法 |
|---|---|
| 加一个接口 | `api.py` 里加路由 → 在 `/api/v1/ai/manifest` 的端点清单里补一条 → 文档 `04-接口文档.md` 补一行 |
| 加一个 MCP 工具 | `mcp_server.py` 里用 `@tool(...)` 装饰器，**说明里写清"什么时候该用它"** |
| 改错误码 | 同步更新 `docs/04-接口文档.md` §9 的错误码全表 |
| 改包格式 | 提升 `bundle.FORMAT_VERSION`，并在 `docs/06-同步与加密.md` 同步字节表 |
| 改配置项 | 同时更新 `docs/03-部署指南.md` 的配置表（`.env` 也可配，见 §4.0） |

**提交前必须做**（一条命令跑完全部七项，会自己配好前提条件）：

```bash
bash scripts/check-all.sh                 # 工作区代码：**八项**全绿才算可交付（末尾给总表）
bash scripts/verify-release.sh v0.8.0     # 发布后：下载产物 → 跑它的 --selftest → 用它起 core 跑完整 e2e
```

> 第二个别省。`check-all.sh` 验的是源码，用户拿到的是 Releases 上的二进制 ——
> 打包环节出问题（缺库、glibc 版本、路径写错）源码测试完全看不出来。
> 历史上踩过：ubuntu-latest 打出来的包要求 GLIBC_2.42，在 Ubuntu 22 上直接跑不起来。
>
> **下载不通时怎么办**：GitHub 对这台机器的下载经常限速/截断（`verify-release.sh`
> 的大小校验会拦住）。可以直接按 CI 的方式本地打包再验，效果等价：
> `python -m PyInstaller packaging/zhiwen-client.spec --noconfirm`
> 然后 `dist/zhiwen-client --selftest` + `dist/zhiwen-client serve` 跑 e2e。
> 实测：0.7.69 的本地打包产物 selftest 14 项 + e2e 73 项全过。

想逐项跑（排错时用）：

```bash
.venv/bin/python -m pytest tests -q            # 601 passed, 2 skipped
.venv/bin/python scripts/e2e-check.py          # 73 通过 / 0 失败 / 3 跳过（配主密码+hub+令牌+批准设备）
.venv/bin/python scripts/e2e-cross-machine.py  # 30 通过 / 0 失败（两台真机器 + hub）
node scripts/gateway-check.cjs                 # 21 通过 / 0 失败（网关接管）
node scripts/ui-check.cjs                      # 29 通过 / 0 失败（客户端控制台，含扩展包界面）
node scripts/console-ui-check.cjs              # 81 通过 / 0 失败（hub 主控台，mock 后端）
DISPLAY=:78 .venv/bin/python scripts/gui-check.py  # 35 通过 / 0 失败（客户端 tkinter GUI，含真改代理、扫描本机）
ZW_CHROME=<chrome> .venv/bin/python scripts/tabs-check.py  # 13 通过 / 0 失败（检测页 + 标签恢复）
```

> 这三条是最常用来判断"能不能交付"的：
> `pytest`（逻辑）、`e2e-check.py`（单机全能力，走真 HTTP + 真浏览器）、
> `console-ui-check.cjs`（主控台界面，真浏览器 + mock 后端）。
> 数字对不上就说明有用例过时了 —— 要么改代码，要么改断言，别放着红。

---

## 六、危险操作清单（做之前先说明并等确认）

- 改 `bundle.py` 的白/黑名单
- 改 `sync.py` 的门禁顺序
- 改 `manager.py` 的租约逻辑
- 动 `hub/store.py` 的 CAS / 索引
- 任何会删数据的操作（`profile delete --purge`、`maintenance cleanup`）

---

## 七、当前状态与待办

**已验证**（2026-11-17，Linux + 云电脑 Windows 实机）：
- 601 单元测试、**73 项单机端到端（0 失败 / 3 跳过）**、**30 项跨机端到端**、81 条主控台断言、29 条客户端控制台断言、35 条客户端 GUI 断言、20 项网关接管 —— 全绿
- hub **0.7.70** 在 3.5 的 Docker 里跑着；主页/监控/代理库/实时终端都通

**在真机上验过的**（云电脑 Windows 10/11 + Chrome 157）：
- 免授权码审批流程（pending → 服务器点允许 → 客户端自动进入）
- 客户端控制台本机免令牌、远程凭机器码（401 / 200 都实测）
- 坏代理串被拒并给出可读原因（`66.80.12 0.66` 的典型笔误）
- 带认证代理走本机中继（`--proxy-server=http://127.0.0.1:<port>`，中继有真实连接）
- 启动打开检测页、停止记住标签、再启动恢复且不重复
  （真机复验：初始 3 页 → 启动后 CDP 读到 3 页（ip123 跳到 /zh/）→ 停止记 3 条 →
  再启动**曾出现 4 页重复**，已修；修法与边界见 CHANGELOG 0.7.64）
- 扩展探针在该机器 Chrome 157 上判 `extensions_active=false`（官方内核不给加载）
- 代理库上行/下行双向同步（云电脑加的传到 3.5；3.5 加的拉回云电脑）
- **商店装扩展整条链**（经 mihomo 下载 CRX 164717B → 装 → 建包 → 设默认 → 新建环境自动带上）
- **从 Chrome 用户数据导入「插件 + 数据」**（`with_data=1`、`data_dir` 落地，真实 Windows 环境）
- **创建后改代理**（无认证→直连上游 `--proxy-server=socks5://…`；带密码→本机中继
  `--proxy-server=http://127.0.0.1:<port>` + warnings 如实说明），改完立即生效
- `extensions_active=False`（Chrome 157 忽略 --load-extension，系统如实回报）
- **本机免令牌（真机回环）**：`curl 127.0.0.1:8730/api/v1/meta` 不带令牌 →
  `auth_required=False`，而 host 是 `0.0.0.0`（按请求判定，不是按配置）
- **版本落后徽章 / 离线陈旧状态**：在线时状态原样显示；离线时变「上次：…」
- **三个默认检测页确实对得上用途**（云电脑经代理实测，均 200）：
  `browserleaks.com/ip`（IP）、`ip123.in`（IP 归属/干净度）、`creepjs`（浏览器指纹）

- **商店链接直接装扩展（整条链，A/B 对照）**：
  不配 `ZW_DOWNLOAD_PROXY` → `EXT_STORE_UNREACHABLE`（ConnectTimeout）+ 可操作提示；
  配上（借用云电脑的 mihomo `:21080`）→ **成功下载 163395 字节**、
  自动建包 `is_default=true`。这台机器直连 Google 必然超时，
  所以"成功只能来自代理"，因果关系闭合。

**未验证**：指纹能否过 browserscan、fingerprint-chromium 的种子一致性、
带认证的**真实**上游代理握手（落地代理只能经前置访问，本仓库所在网络连不到）。

**待办（按优先级）**：
1. RXYCDP 的扩展实物导入：**扫描→导入→默认带上整链已在云电脑真机跑通**
   （扫到 Edge/Chrome；用扫到的路径导入 2 个扩展、`as_default` 生效、
   新建环境自动带上）。仍缺的是"那台装着 RXYCDP 的机器"本身
   —— 云电脑上确认没有这个浏览器，需要用户在自己机器上点一次「扫描本机」
3. `zw sync rekey`（换主密码逐包重加密；碰主密码与加密快照，属危险区，需用户明确要求）
4. hub 多用户/权限

---

## 八、和用户沟通的风格

- 结论先行，再给依据。
- 说清楚"我改了什么" vs "我验证了什么"，不要把两者混在一起。
- 失败、部分成功、跳过，如实说。
- 不要问"要不要我继续"，一次做完。
