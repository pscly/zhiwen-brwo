# 知纹 · zhiwen

> 个人专用的**指纹浏览器机群管理器**：在一台机器上管理任意多个隔离的浏览器环境，把调试端口安全地给局域网用，并且能在多台电脑之间同步登录态。
> 全中文文档 · AI 原生（MCP + 自描述端点）· Windows / Linux 双平台 · 局域网内零 HTTPS 依赖

版本 **0.2.0**。相对 0.1.0 修了 20 多个真实缺陷、补了代理库/扩展库/分组/导出导入/指纹体检/网段白名单等能力，
并把控制台重写成可用的一体化面板。逐条清单见 [CHANGELOG](CHANGELOG.md)，
"哪些验过、哪些没验"见 [09-交接说明](docs/09-交接说明.md)。

---

## 这是什么

市面上的指纹浏览器（Roxy / AdsPower / GoLogin…）都卡在一个地方：**免费额度只给几个窗口，而自动化接口往往还要另外付费**。

知纹换了一条路：**指纹引擎用开源的，管控层自己造。**

```
┌──────────────────────────────────────────────┐
│  zw-core（每台电脑一个）                      │
│   · 环境编排：建/开/关/重启/批量/租约          │
│   · CDP 网关：把只绑 127.0.0.1 的调试端口      │
│                安全暴露给局域网                │
│   · 数据桥：cookie / localStorage 明文读写     │
│   · 代理：库 + 凭据注入（带密码的代理也能用）  │
│   · AI 层：MCP 工具 + 自描述端点               │
└──────────────────┬───────────────────────────┘
                   │ 局域网 HTTP（包体自带 AEAD 加密）
┌──────────────────▼───────────────────────────┐
│  zw-hub（一台就够，建议放 NAS / 小主机）        │
│   · 加密快照存储（CAS，绝不静默覆盖）           │
│   · 跨机租约（防两台机器同时用一个号）          │
│   · 全 Docker，一个容器一个卷                   │
└──────────────────────────────────────────────┘
```

指纹本身由 **fingerprint-chromium**（BSD-3，C++ 引擎级指纹，`--fingerprint=<种子>` 可持久化）或你系统里已有的 Chrome/Chromium 负责 —— 知纹不重复造这个轮子。

## 四个核心特性

### 1. 无限环境，一个入口

控制台、CLI、MCP 三条路都能管同一批环境，改任何一边另一边立刻看到。

```bash
zw profile create --name 小红一号 --timezone Asia/Shanghai --lang zh-CN
zw profile start 小红一号
zw profile cdp   小红一号
zw profile probe 小红一号        # 指纹体检：读真机实际暴露的值并指出自相矛盾处
```

每条命令都能加 `--json`，输出给脚本或 AI 直接消费。

### 2. CDP 能出局域网（这是全行业的空白）

Chrome 只肯监听 `127.0.0.1`，而且它的发现接口里**写死了回环地址**，所以"把 CDP 转发出去"不是加个 nginx 就行 —— 必须重写响应。

```bash
# 在另一台机器上
curl -s 'http://<core>:8730/cdp/<环境id>/json/version?token=...' | jq -r .webSocketDebuggerUrl
# → ws://<core>:8730/cdp/<环境id>/devtools/browser/<GUID>?token=...   ← 地址已被改写
```

然后 Playwright / Puppeteer 直接接管：

```js
const browser = await chromium.connectOverCDP(gateway_ws_url);
```

### 3. 跨机同步登录态 —— 但**不搬文件**

这是最容易做错的地方。Chromium 的 `Cookies` 和 `Login Data` 用操作系统密钥加密（Windows DPAPI / macOS Keychain），**直接拷目录到另一台机器必然解不开**；更糟的是 Chromium 解不开时会把 cookie 当"损坏"删掉，一次错配就全线掉登录。

知纹的做法：

```
源机：用 CDP 把运行中的 cookie 以明文取出来 → 打包（排除加密文件）→ AEAD 加密 → 推到 hub
目标机：校验密钥指纹（不一致立刻中止，一个字节都不落盘）→ 原子换目录 → 用 CDP 把 cookie 写回去
```

包内还带**环境自身的设置**（代理、扩展、窗口、时区、分组、标签），所以换台机器拉过去就能直接用，不是一个"裸环境"。

### 4. 带密码的代理真的能用

Chromium 的 `--proxy-server` **不支持用户名密码**：写成 `http://user:pass@host:port` 时它会把整串当主机名，结果连不上而且报错完全看不出原因。

知纹会自动为带凭据的代理生成一个 MV3 认证扩展（挂 `chrome.proxy` + `onAuthRequired`），和用户扩展一起加载。凭据不会进命令行、不会回显到接口响应、也不会写进日志。

## 界面

控制台是单文件、零构建、无外部依赖（内网离线环境要求），深色/浅色双主题。

| 总览 | 环境列表 |
|---|---|
| ![总览（深色）](docs/screenshots/01-总览-深色.png) | ![环境（深色）](docs/screenshots/02-环境-深色.png) |
| **指纹体检**（详情抽屉里读真机指纹并指出自相矛盾处） | **数据**（cookie / localStorage / 页面操作） |
| ![指纹体检](docs/screenshots/10-指纹体检.png) | ![数据](docs/screenshots/03-数据-深色.png) |

其余截图在 [docs/screenshots/](docs/screenshots/)：总览浅色、环境浅色、新建表单、同步、运维。

---

## 已验证 / 未验证

> 下表是**真跑过**的结果。测试环境：Ubuntu 24.04 / 14 核 / 28G / 无 GPU，Chromium 150；
> 跨机部分是 192.168.3.6 ↔ 192.168.3.5 两台真机器，中间过真实局域网。

| 项 | 结果 |
|---|---|
| 单元测试 | ✅ **142 项**全通过（`pytest tests -q`，需先装 dev 依赖，见下） |
| 单机全能力自检 | ✅ **63 项**通过、0 失败、3 项按环境跳过（`scripts/e2e-check.py`） |
| 跨机端到端（两台真机器 + 真 hub） | ✅ **30 项**全通过（`scripts/e2e-cross-machine.py`） |
| 网关接管（Playwright over CDP） | ✅ **20 项**全通过（`scripts/gateway-check.cjs`） |
| Web 控制台 | ✅ 七个视图 + 新建表单 + 详情抽屉，深色/浅色两套主题，真实浏览器点通、console 零报错 |
| 建环境 → 启动浏览器 → 拿 CDP → 地址重写 | ✅ |
| cookie 写 / 读 / netscape 导出 / 清空 | ✅ |
| localStorage 写 / 读（在专属管理页里，不劫持用户页面） | ✅ |
| 页面导航 / 可见文字 / 执行 JS / 截图 | ✅ |
| 推到 hub（真实网络上的 3.5）→ 另一台机器拉取 → 自动导入 → cookie 真的还原 | ✅ |
| 跨机搬运环境设置（代理/窗口/时区/分组/种子） | ✅ |
| 错主密码必须在落盘前被拦 | ✅ 返回 `412 KEY_FINGERPRINT_MISMATCH`，本地不留半成品 |
| 导出 `.zwp` → 删除 → 导入还原 | ✅ |
| 代理库增删查 + 出口检测 + 凭据不泄漏 | ✅ |
| 扩展库安装/绑定/解绑/删除 + 被绑定时的保护 | ✅ |
| 带凭据代理走认证扩展 | ⚠️ 扩展生成与注入路径已验证；**真实代理的认证握手未实测**（本机没有可用代理） |
| MCP 工具清单 + 调用 + 鉴权 | ✅ |
| **Windows 实机整体跑通** | ⬜ **未验证**。代码路径已按 Windows 分支单测（`test_windows_parity.py`），但没在真 Windows 上跑过 |
| 指纹能否过 browserscan | ⬜ 未验证。无 GPU 的 Linux 上 WebGL 拿不到真实显卡，必须到有显卡的 Windows 上测 |
| 指纹种子的一致性（同种子两次启动是否同指纹） | ⬜ 未验证。当前测试机上的 Chromium 不是 fingerprint-chromium，`--fingerprint` 被忽略 |

## 五分钟上手

### Windows（推荐路径）

```powershell
git clone https://github.com/pscly/zhiwen-brwo.git C:\dev\zhiwen_brwo
cd C:\dev\zhiwen_brwo
.\scripts\windows\部署.cmd
```

它会建虚拟环境、装依赖、**随机生成**访问令牌与同步主密码写进 `.env`、自动找系统里的 Chrome，然后启动服务。
控制台在 <http://127.0.0.1:8730>。详见 [快速开始](docs/01-快速开始.md)。

### Linux

```bash
git clone https://github.com/pscly/zhiwen-brwo.git && cd zhiwen_brwo
uv venv .venv && uv pip install --python .venv/bin/python -e ./core -e ./hub

export ZW_CHROME_BIN=/path/to/chrome      # 或用系统自带的 chrome/chromium
export ZW_ADMIN_TOKEN=换成你的令牌
.venv/bin/python -m zw_core.main          # 打开 http://127.0.0.1:8730
```

### 同步中心（可选，但强烈建议）

```bash
cd hub && cp .env.example .env       # 填一个足够长的令牌
sudo docker compose up -d
```

已经部署在 `192.168.3.5:8700`，见 [部署指南](docs/03-部署指南.md)。

## 给 AI 用

知纹从一开始就是按"AI 是主要使用者"设计的，不只是暴露接口：

| 入口 | 说明 |
|---|---|
| `GET /api/v1/ai/manifest` | **自描述端点**：AI 一进来就知道这是什么、怎么调、错误码怎么解，不用先读文档 |
| `zw mcp` | MCP over stdio，Claude Desktop / Cursor / Codex 直接挂载（进程内直调，无需令牌） |
| `POST /mcp` | MCP over HTTP，远端 AI 可用（**需要 Bearer 令牌**） |
| `AGENTS.md` / `SKILL.md` | 仓库内给 AI 的工作手册与技能描述 |
| 统一错误形状 | `{ok:false, code, message, hint}`，`code` 稳定可编程，`hint` 给人看 |

```json
{"mcpServers": {"zhiwen": {"url": "http://192.168.3.6:8730/mcp",
  "headers": {"Authorization": "Bearer <ZW_ADMIN_TOKEN>"}}}}
```

详见 [AI 接入指南](docs/05-AI接入指南.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [01-快速开始](docs/01-快速开始.md) | Windows / Linux 两条路径，含一键脚本 |
| [02-架构设计](docs/02-架构设计.md) | 分层、状态机、五条数据流、为什么这么设计 |
| [03-部署指南](docs/03-部署指南.md) | hub 上 Docker、core 裸装、多机拓扑、开机自启 |
| [04-接口文档](docs/04-接口文档.md) | 全部 REST / WS 端点、错误码表、示例 |
| [05-AI接入指南](docs/05-AI接入指南.md) | MCP、自描述端点、给 AI 的提示词模板 |
| [06-同步与加密](docs/06-同步与加密.md) | 包格式、白黑名单、密钥指纹门禁、冲突处理 |
| [07-排错手册](docs/07-排错手册.md) | 按现象查原因的排查表 |
| [08-运维与日志](docs/08-运维与日志.md) | 日志在哪、怎么看、怎么归档 |
| [09-交接说明](docs/09-交接说明.md) | **明天怎么验收**，以及哪些没验过 |
| [10-前端对接契约](docs/10-前端对接契约.md) | 前端与后端的接口形状（改端点先改这里） |
| [CHANGELOG](CHANGELOG.md) | 0.2.0 相对 0.1.0 逐条改了什么 |

## 自检脚本

先装测试依赖（`pyproject.toml` 里它们是可选依赖，默认不装 —— 生产环境不需要 pytest）：

```bash
uv pip install --python .venv/bin/python -e "./core[dev]" pytest-asyncio
# 或者：uv pip install --python .venv/bin/python pytest pytest-asyncio
```

然后：

```bash
# 单机：真开浏览器，把全部能力过一遍（63 项）
ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xxx ZW_SYNC_PASSWORD=xxx \
  python scripts/e2e-check.py

# 跨机：两台 core + 一台 hub（30 项）
ZW_A=http://192.168.3.6:8730 ZW_TA=xx ZW_B=http://192.168.3.5:8731 ZW_TB=xx \
  ZW_SYNC_PASSWORD=xx python scripts/e2e-cross-machine.py

# 网关：Playwright 通过网关接管（20 项）
ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xx node scripts/gateway-check.cjs
```

## 许可

MIT。见 [LICENSE](LICENSE)。

## 已知限制（写在最前面，别当成 bug）

1. **指纹质量取决于内核，不取决于本项目**。知纹管的是编排与同步，不是伪造指纹。用系统 Chrome 时 `--fingerprint` 会被忽略，多环境之间只剩 cookie 隔离。
2. **无 GPU 的机器上 WebGL 拿不到真实显卡**（实测 `getContext('webgl')` 只能拿到 SwiftShader）。承担"真人身份"的环境请放在有显卡的机器上。
3. **同一环境同一时刻只能一个持有者**。这是刻意的，为了不让两台机器同时用一个号。
4. **主密码丢了，远端快照就解不开了**。端到端加密的必然代价。
5. **传输层是明文 HTTP**（局域网）。机密性靠包体自带加密，完整性靠 AEAD 认证标签。跨公网请走隧道，别直接开端口。
6. **控制面与浏览器不是同一个进程**。core 重启不会杀掉正在跑的浏览器（启动时会重新接管），但 core 挂了期间没人做健康巡检。
