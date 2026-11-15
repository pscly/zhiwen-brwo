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
tests/   19 项单元测试
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

---

## 四、跑起来

```bash
# 依赖
uv venv .venv && uv pip install --python .venv/bin/python -e ./core -e ./hub

# 测试（必须全绿才算改对）
.venv/bin/python -m pytest tests -q

# 开发服务（pidfile 管理，不要用 pkill 免得误杀自己的 shell）
bash scripts/run-dev.sh
bash scripts/stop-dev.sh

# 端到端（需要两个 core 在 8730 / 8731，hub 可达）
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
| 改配置项 | 同时更新 `docs/03-部署指南.md` 的配置表 |

**提交前必须做**：
```bash
.venv/bin/python -m pytest tests -q          # 19 passed
.venv/bin/python scripts/e2e-check.py        # 16 项全过
```

---

## 六、危险操作清单（做之前先说明并等确认）

- 改 `bundle.py` 的白/黑名单
- 改 `sync.py` 的门禁顺序
- 改 `manager.py` 的租约逻辑
- 动 `hub/store.py` 的 CAS / 索引
- 任何会删数据的操作（`profile delete --purge`、`maintenance cleanup`）

---

## 七、当前状态与待办

**已验证**：Linux 上 19 单元 + 16 端到端全过；hub 在 3.5 运行中。
**未验证**：Windows 实机（用户在测）、指纹能否过 browserscan、Windows 跨机 CDP。

**待办（按优先级）**：
1. `zw sync rekey`（换主密码逐包重加密）
2. 按环境绑定扩展的 UI
3. hub 多用户/权限
4. Windows 上 `--fingerprint` 种子一致性实测

---

## 八、和用户沟通的风格

- 结论先行，再给依据。
- 说清楚"我改了什么" vs "我验证了什么"，不要把两者混在一起。
- 失败、部分成功、跳过，如实说。
- 不要问"要不要我继续"，一次做完。
