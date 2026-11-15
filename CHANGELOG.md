# CHANGELOG

## 0.2.0 — 2026-11-16

这一版的目标是「明天直接能用的成品」，所以重点不在加功能，而在**把 0.1.0 里会说谎、
会静默失效、会毁数据的地方全部挖出来修掉**，然后补上真正缺的那几块能力。

下面每一条都对应一次实跑复现，不是"顺手优化"。

---

### 会被当成 bug 的崩溃与失效

| # | 问题 | 现象 | 修法 |
|---|---|---|---|
| 1 | **`zw profile cdp` 必然崩溃** | 读 `data["gateway_ws_base"]`，而这个键在响应里从不存在 → `KeyError` | 改成打印真实可用的 ws 地址 + Playwright 片段 + 令牌剩余时间 |
| 2 | **`POST /api/v1/sync/all/push` 从未生效** | 被 `/sync/{ref}/push` 抢先匹配，当成"环境名叫 all"，返回 `PROFILE_NOT_FOUND` | 固定路径挪到通配路径之前；路径改为 `/sync/push-all`；加回归测试锁死顺序 |
| 3 | **`POST /api/v1/profiles/import` 同理被吃掉** | 被 `/profiles/{ref}` 匹配 | 同上，并在 api.py 顶部写清"固定路径必须在通配之前"的规矩 |
| 4 | **页面类端点全部 422** | `page/navigate`、`page/screenshot`、`page/evaluate` 请求体模型定义在 `build_app()` 内部，配合 `from __future__ import annotations` 后 FastAPI 解析不到字符串注解 | 模型提到模块级 |
| 5 | **`fp_timezone` 从未生效** | 字段躺在数据库里，从没进过命令行或环境变量 | 下发 `--timezone=` 与子进程 `TZ`；`--accept-lang` 在只给 `lang` 时自动补 |
| 6 | **`fp_accept_lang` / `fp_platform_version` / `fp_disable_spoofing` 缺失** | 没有对应字段与参数 | 新增三列（含 DB 迁移）+ 三个命令行参数 |
| 7 | **带密码的代理会静默连不上** | Chromium 的 `--proxy-server` 不支持认证，`u:p@host` 会被当主机名 | 新增 `services/proxy.py`：自动生成 MV3 认证扩展，凭据不进命令行 |
| 8 | **MCP over HTTP 完全无鉴权** | 任何能连 8730 的人都能通过 MCP 读 cookie、截图、开浏览器 | 与 REST 共用 Bearer 令牌 + 网段白名单 |
| 9 | **代理密码会回显给前端** | `POST /profiles` 把整串带密码的代理地址原样存进 `proxy` 字段并返回 | 入库前剥离凭据，只留上游地址；密码只存代理库 |
| 10 | **hub 的 `allowed_cidrs` 是个摆设** | 配置项存在但从未被检查，「只允许局域网」的意图其实没生效 | 真正实现，且 fail-closed（解析不出 IP 也拒） |
| 11 | **环境停止后仍显示旧端口** | `cdp_port` 没清，控制台/CLI 显示一个已经属于别人的端口 | stop/failed 时清空 |

### 会毁数据 / 毁账号的问题

| # | 问题 | 为什么危险 | 修法 |
|---|---|---|---|
| 12 | **端口串号** | 只要分配的端口上有"别的" chrome 在响应，就会把**别人浏览器**的 ws 登记成自己的路由，客户端连过去看到另一个环境的 cookie | 等就绪时校验"监听该端口的进程是不是我们刚拉起的那个"（`ss`/`netstat`/`lsof` + `/proc/<pid>/cmdline` 交叉核对）；不通过就 `PORT_MISMATCH` 中止。同时加 `--remote-debugging-address=127.0.0.1` 明确绑回环 |
| 13 | **页面操作劫持用户标签页** | 读 localStorage 时会把**你正在看的页面**导航走 | 引入"专属管理标签页"：页面类操作一律跑在那里，cookie 读写只 attach 不导航 |
| 14 | **异常退出后残留锁文件** | `SingletonLock` / `DevToolsActivePort` 留着会让下次启动直接失败 | 启动前清理（只清这几个，不动用户数据） |
| 15 | **崩溃自动重启无上限** | 一个坏内核能把 CPU 打满 | 加 `restart_count` + 退避 + 上限（默认 3 次） |
| 16 | **导出/同步丢掉环境设置** | 换台机器拉过去是个"裸环境"：代理、扩展、窗口、时区全丢，得手动重建 | bundle manifest 增加 `profile` 段，导出/推送/拉取都带上并还原 |
| 17 | **DB 升级会缺列** | 新字段只写进迁移，没写进建表 DDL → 全新库缺列、老库靠迁移补齐，两条路径不一致 | DDL 与迁移都补齐，并加测试 |

### 新增能力

- **代理库**：`/api/v1/proxies` CRUD + 单条/未保存出口检测（多源回环，任一成功即可）。被引用时拒删。
- **扩展库全链路**：环境里存 slug（兼容老数据的绝对路径），启动时解析并给出缺失告警；安装支持目录与 zip（含 zip 路径穿越防护、多套一层目录的自动纠正）；被环境绑定时拒删。
- **分组**：`/api/v1/groups`，控制台按分组过滤。
- **导出 / 导入单文件**：`POST /profiles/{id}/export` → `.zwp`（加密、带环境设置），`POST /profiles/import` 还原。适合只有一台机器或想留离线备份。
- **指纹体检**：`POST /profiles/{id}/fingerprint/probe` —— 读真机实际暴露的 UA / platform / UA-CH / 时区 / languages / CPU / 内存 / WebGL / canvas hash / audio hash / 字体数 / webdriver / 屏幕，并给出"自相矛盾"警告（声明 windows 但 platform 是 Linux、时区与设定不符、webdriver=true、SwiftShader 软件渲染……）。
- **cookie 清空**、**批量操作真并发**、**网段白名单**、**审计表自动裁剪**、**空闲回收**（有活连接时绝不回收）。
- **CLI** 补齐 `probe / export / import / bind-ext / unbind-ext / proxy * / sync push-all / sync rm / group / events / audit / trim / cleanup / reap`，所有子命令统一支持 `--json`。
- **MCP** 从 23 个工具扩到 26 个（增 `zw_update_profile`、`zw_fingerprint_probe`、`zw_clear_cookies`、`zw_list_proxies`、`zw_check_proxy`），并让错误返回带稳定 `code`。

### 控制台

从"能看个列表"重写成可用面板：左侧窄侧栏 + 右侧内容区，苹果风液态玻璃质感，深色/浅色双主题。

七个视图：总览 / 环境 / 数据 / 扩展 / 代理 / 同步 / 运维，全部后端能力都接进了 UI。
自定义模态与抽屉（不再用 `prompt()`），危险操作二次确认（重置指纹要求输入环境名），
错误一律可见（带 code / message / hint，可展开），轮询合并为单个定时器且页面隐藏时暂停。

### 测试

| 类型 | 0.1.0 | 0.2.0 |
|---|---|---|
| 单元测试 | 45 | **136** |
| 单机全能力端到端 | 16（本机双进程模拟） | **63**（真开浏览器、真连 hub） |
| 跨机端到端 | — | **30**（3.6 ↔ 3.5 两台真机器） |
| 网关接管（Playwright） | — | **20** |
| 控制台交互 | 4 步 | **21 项**（七视图 + 表单 + 详情 + 双主题 + XSS 转义 + 导出文件名） |

新增测试文件：`test_sync_integration.py`（真起 hub 子进程）、`test_hub_http.py`、`test_proxy.py`、
`test_engines_extra.py`、`test_windows_parity.py`。

### 文档

- 新增 [CHANGELOG](CHANGELOG.md)、[10-前端对接契约](docs/10-前端对接契约.md)。
- 重写 [01-快速开始](docs/01-快速开始.md)、[04-接口文档](docs/04-接口文档.md)、[09-交接说明](docs/09-交接说明.md)。
- Windows 脚本重做：`部署.cmd`（自动生成随机令牌与主密码、自动找内核）、`启动.cmd`、
  `自检.cmd`、`全量自检.cmd`、`安装字体与依赖说明.txt`。

---

## 0.1.0 — 2026-11-16

首个版本：core（编排 + CDP 网关 + 同步客户端）、hub（CAS + 跨机租约）、
CLI、Web 控制台、MCP、9 篇中文文档、Linux 上 45 项单测与 16 项端到端全过。
