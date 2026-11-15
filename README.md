# 知纹 · zhiwen

> 个人专用的**指纹浏览器机群管理器**：在一台机器上管理任意多个隔离的浏览器环境，把调试端口安全地给局域网用，并且能在多台电脑之间同步登录态。
> 全中文文档 · AI 原生（MCP + 自描述端点）· Windows / Linux 双平台 · 局域网内零 HTTPS 依赖

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
│   · AI 层：MCP 23 个工具 + 自描述端点          │
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

## 三个核心特性

### 1. 无限环境，一个入口

```bash
zw profile create --name 小红一号
zw profile start 小红一号
zw profile cdp   小红一号
```

每条命令都能加 `--json`，输出给脚本或 AI 直接消费。

### 2. CDP 能出局域网（这是全行业的空白）

Chrome 只肯监听 `127.0.0.1`，而且它的发现接口里**写死了回环地址**，所以"把 CDP 转发出去"不是加个 nginx 就行 —— 必须重写响应。

知纹的网关做的正是这件事：

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

## 已验证 / 未验证

> 下表是**真跑过**的结果，不是设计意图。测试环境：Ubuntu 24.04 / 14 核 / 28G / 无 GPU，Chromium 150。

| 项 | 结果 |
|---|---|
| 单元测试 | ✅ 19 项全通过 |
| 端到端自检（16 项） | ✅ 全通过（见 `scripts/e2e-check.py`） |
| 建环境 → 启动浏览器 → 拿 CDP | ✅ |
| 网关地址重写 → Playwright `connectOverCDP` 接管 | ✅ 跨进程实测 |
| cookie 写 / 读 / netscape 导出 | ✅ |
| 推到 hub（真实网络上的 3.5） | ✅ |
| 另一台机器拉取 → **自动导入** → cookie 真的还原 | ✅ |
| 错主密码必须在落盘前被拦 | ✅ 返回 `412 KEY_FINGERPRINT_MISMATCH` |
| MCP 工具清单 + 调用 | ✅ 23 个工具 |
| **Windows 上的完整验证** | ⬜ 待你在 Windows 上跑（见 [交接说明](docs/09-交接说明.md)） |
| **指纹能否过 browserscan** | ⬜ 未验证。无 GPU 的 Linux 上 WebGL 拿不到，必须到有显卡的 Windows 上测 |

## 五分钟上手

### Linux（已验证）

```bash
git clone <本仓库> && cd zhiwen_brwo
uv venv .venv && uv pip install --python .venv/bin/python -e ./core -e ./hub

# 指定内核（或用系统自带的 chrome/chromium）
export ZW_CHROME_BIN=/path/to/chrome
export ZW_ADMIN_TOKEN=换成你的令牌

.venv/bin/python -m zw_core.main    # 打开 http://127.0.0.1:8730
```

### Windows

见 [快速开始](docs/01-快速开始.md)，有专门的一节和一键脚本 `scripts/windows/启动.cmd`。

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
| `zw mcp` | MCP over stdio，Claude Desktop / Cursor / Codex 直接挂载 |
| `POST /mcp` | MCP over HTTP，远端 AI 可用 |
| `AGENTS.md` / `SKILL.md` | 仓库内给 AI 的工作手册与技能描述 |
| 统一错误形状 | `{ok:false, code, message, hint}`，`code` 稳定可编程，`hint` 给人看 |

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
| [09-交接说明](docs/09-交接说明.md) | **明天在 Windows 上怎么验收** |

## 许可

MIT。见 [LICENSE](LICENSE)。

## 已知限制（写在最前面，别当成 bug）

1. **指纹质量取决于内核，不取决于本项目**。知纹管的是编排与同步，不是伪造指纹。
2. **无 GPU 的机器上 WebGL 拿不到**（实测 `getContext('webgl')` 返回 `null`）。承担"真人身份"的环境请放在有显卡的机器上。
3. **同一环境同一时刻只能一个持有者**。这是刻意的，为了不让两台机器同时用一个号。
4. **主密码丢了，远端快照就解不开了**。端到端加密的必然代价。
5. **传输层是明文 HTTP**（局域网，按你的要求）。机密性靠包体自带加密，完整性靠 AEAD 认证标签。
