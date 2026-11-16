# CHANGELOG

## 0.3.2 — 2026-11-16

修一个**会让 Windows 用户第一次打开就失败**的缺陷，并把服务器面板做成可点入口。

### 修：`ValueError: Unable to configure formatter 'default'`

云电脑上真机复现的报错：

```
本机服务启动失败：
ValueError: Unable to configure formatter 'default'
```

根因不是配置问题，是**打包形态**问题：Windows 用 `console=False` 打包后
`sys.stdout` / `sys.stderr` 都是 `None`。而 uvicorn 默认会用 `dictConfig`
配日志，它的 `default` formatter 构造时要调 `sys.stdout.isatty()`
→ `AttributeError`，`dictConfig` 把它包成那句 `ValueError`。

两处修复：
- `uvicorn.Config(..., log_config=None)`：不让 uvicorn 重配日志
  （我们自己的 `setup_logging()` 已经管好了）。`main.py` 的 `uvicorn.run` 同样处理。
- `setup_logging()` 拿不到可用的流时**不装 StreamHandler**，只留文件日志
  —— 否则第一次写日志就会在 `Handler.emit` 里炸。

本地复现方式（已写成测试）：把 `sys.stdout`/`sys.stderr` 都置 `None` 再启动
`LocalCore`。修复前必抛那句 ValueError，修复后正常启动并响应 `/healthz`。

### 自检新增一项：真的启动一次本机服务

`--selftest` 原来只检查 `create_app()` 能构造，**不启动 uvicorn** ——
所以这个 bug 在自检里完全看不出来（它只在真启动时才炸）。

现在新增 `本机服务可启动（无控制台）`：刻意把标准流置 `None`（模拟 Windows
GUI 子程序），真起一次 uvicorn，访问 `/healthz`，再关掉。自检从 11 项变 12 项。

> 这是这次最值钱的一条：自检和真实启动路径不一致时，自检就是安慰剂。

### 客户端：服务器面板做成可点入口

用户反馈"不知道怎么打开 3.5 的管理控制台"。现在：
- 连接信息卡里有「服务器面板」按钮
- 底部动作栏里有「服务器面板」按钮（未连接时禁用）
- 连上时事件流直接打出 `服务器管理面板：http://<服务器>/panel`
- 首次配置页也写明"管理面板在：服务器地址 + /panel"

### 测试

222 → **226 项**。新增 `tests/test_no_console.py`（4 项）：
无流时 `setup_logging` 不崩且仍写文件、`LocalCore` 确实传了 `log_config=None`、
无流时能真启动并响应 `/healthz`、无流时统计接口可用。

顺带修了一处测试间污染：`LocalCore.start(device_id=...)` 会写全局
`ZW_DEVICE_ID`，不清理会让后续同步测试带着"未注册设备"去请求 hub
（报 `设备未注册：zwd_testabcdefgh`）。新测试的 fixture 现在会隔离它。

## 0.3.1 — 2026-11-16

在 0.3.0 基础上把「日常真的能用」这层补齐。

### 客户端

- **界面重做**（拆成独立模块 `zw_core/gui.py`，业务逻辑仍在 `client.py`）：
  顶栏状态、连接信息卡（机器码可复制）、本机状态四个数（环境/运行中/端口/磁盘，
  5 秒刷新）、带时间戳与颜色的事件流、底部动作栏。
- **关窗口 ≠ 退出**：默认最小化，本机服务继续跑；「退出」才真停并二次确认。
  有 pystray 时自动挂托盘（右键可开控制台/显示窗口/退出），没有就降级。
- **自检按钮**：界面里直接跑，结果弹窗 + 写 `selftest.txt`，不用开命令行。
- **开机自启开关**：界面里的勾选框，随时能关。Windows 写 `HKCU\...\Run`，
  Linux 写 `~/.config/autostart/*.desktop`，macOS 写 `LaunchAgents`。
  勾选状态从**真实系统状态**读，从别处改过也不会显示错。
- **心跳带上本机概况**：`profiles / running / disk_human / engine`，
  服务器面板据此显示"这台机器上有几个环境在跑"，不再只有在线/离线。
- 字体探测重写：**按名字匹配优先**，测量只作辅助。
  原实现只看 `measure()`，而 Tk 在部分 X11 环境里对新 Font 对象返回 0，
  会把本来能用的字体判成不可用。现在：
  · 第 1 层 名字匹配（Windows 雅黑/宋体、macOS 苹方、Linux 文泉驿/思源）
  · 第 2 层 关键字模糊（发行版写法差异）
  · 第 3 层 全量测量（只在名字都没命中时才信）
  · 都不行才退回 Tk 默认，并在 stderr + 弹窗明确告知"缺中文字体、怎么装"
  （以前是界面全空白且一声不响，用户只会觉得程序坏了）

### 服务器端

- 设备表新增四列（`profiles / running / disk_human / engine`），**老库自动补列**，不用删库。
- 心跳接收本机概况；空心跳不会把已上报的数字清零。
- 面板加「本机环境」列（`3 个 · 1 运行` + 磁盘）与「环境总数 / 正在运行」两个 KPI
  （只聚合在线设备——离线设备上报的旧数字没意义）。

### Windows 安装包

- 新增 `packaging/windows/zhiwen-client.iss`（Inno Setup 6）：
  开始菜单 + 可选桌面图标 + **可选开机自启（默认不勾）** + 卸载程序；
  中文界面（`[CustomMessages]` + `[Messages]` 覆盖 55 条内置文案，零外部 .isl 依赖）；
  `PrivilegesRequired=lowest` —— 管理员装到 Program Files，普通账号装到用户目录，不弹 UAC。
  **卸载不删 `client.json`**，重装还是同一台设备（机器码在里面）。
- 新增 `packaging/windows/check-iss.py`：35 项静态检查（编码/段落/GUID/路径/任务默认值/
  注册表写法/`[Code]` 签名/消息名是否真实存在…），跨平台可跑，已接进 CI 与发版流程。
  做过负向验证：假 PE 头、拼错消息名、给 autostart 加 `checkedfirst` 都能被抓出来。

### 修掉的真缺陷

| 问题 | 后果 |
|---|---|
| **`hub/Dockerfile` 用 `pip install ... \|\| true`** | 构建机网络一抖，pip 失败被吞，产出一个**没有 fastapi** 的镜像；`compose up` 显示成功但容器在重启循环里。3.5 上的 hub 因此挂过一次。现在失败就是失败，并在构建末尾 `import fastapi` 自检 |
| **compose 只绑 `192.168.3.5:8700`** | 在 3.5 上 `curl 127.0.0.1:8700` 连不上，部署脚本的验证与日常排错都误判成"服务没起来"。现在同时绑回环 |
| **部署脚本验证用 127.0.0.1** | 与上一条叠加，永远验证失败。改为验证**局域网地址**（这才证明"别人能连上"），并额外报一次回环结果 |
| **部署脚本 `build \| tail` 丢退出码** | 管道让 `if` 永远为真，可能拿旧镜像重建。改为显式判断 |
| **镜像在目标机上 build** | 3.5 连 PyPI 极慢（单次 7.4 秒），pip 常断流。改为**本机构建 + save/scp/load**，目标机 `pull_policy: never` 绝不联网 |

### Windows 控制台编码（第三次踩同一个坑，这次从根上治）

CLI / 客户端 / 自检脚本 / 安装脚本检查器都会打印中文与 ✅ ❌ ⚠️。
Windows 默认控制台编码是系统代码页：中文系统 cp936（中文能显示但 ✅ 不行）、
英文系统 cp1252（连中文都不行）。CI 上已因此挂过两次，
而用户 Windows 上的**第一条命令** `zw doctor` 就会崩。

新增 `zw_core/console.py::setup_console()`：把 stdout/stderr 强制 UTF-8，
编不了的字符降级成 `?` 而不是抛异常。CLI、客户端、`e2e-check.py`、
`check-iss.py` 全部接入；CI 两个 workflow 也设了
`PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` 作为双保险。

实测对照：不修的话 `print('✅')` 在 cp1252 下直接
`UnicodeEncodeError: 'charmap' ...`；修了之后正常输出。

### 测试

200 → **222 项**（新增 `test_client_extra.py` 12 项：自启往返/幂等/命令形状、
字体探测返回形状、gui 模块可导入、run_gui 委托、心跳带统计/不带统计；
`test_panel_api.py` 新增 3 项：心跳携带统计、空心跳不清零、被踢设备带统计也拒；
`test_console_encoding.py` 7 项：非 UTF-8 流会被改、UTF-8 不动、force 覆盖、
没有 reconfigure 的流不崩、stdout 为 None 不崩、CLI 入口先修编码）。

另外修了两条**只在 CI 上才暴露**的测试缺陷：
- 自启的 .desktop 断言写成 `"zhiwen" in body` —— 本机能过纯粹因为仓库目录叫
  `zhiwen_brwo`（`sys.executable` 里带这个词），CI 路径不同就失败。
  改为断言产品标记 `zw_core.client` 与 `Name=知纹客户端`。
- 同一条测试只按 Linux 写（断言 XDG 的 .desktop），macOS 上写的是
  LaunchAgents plist，必然失败。改为按平台选路径与断言，并新增
  `test_macos_autostart_plist_branch` 用 monkeypatch 掰 `sys.platform`，
  让 macOS 分支在 Linux 上也能验。

## 0.3.0 — 2026-11-16

把架构从「每台机器各跑一套」扩成「客户端 + 服务器端」，并加了发行包。

### 新增：客户端（桌面程序）

- **首启只要一个输入框：服务器地址。** `192.168.3.5`、`192.168.3.5:8700`、
  `HTTPS://example.com` 都认，自动补协议与默认端口。
- **默认不要对接码。** 连上先问服务器 `GET /v1/enroll-info`，服务器说要不要就怎么来。
- **自动生成机器码**（`zwd_` + 32 字符），存本地 `client.json`（0600），之后一直用。
- 点一次「连接并开始」= 注册设备 → 起本机 core（进程内线程，不用子进程找 python）→ 打开控制台。
- **被踢下线时说人话**：弹窗「这台电脑已被服务器端移除」，而不是抛 403。
- 心跳线程 60 秒一次，被踢时立刻反映到界面。
- `--selftest`：逐项检查打包是否完整（依赖/控制台页面/AEAD 往返/Argon2/配置目录/机器码），
  用在 CI 与「下载后跑不起来」的排查。
- 没有 tkinter 的环境自动退回问答式 CLI，不崩。
- 字体自动探测（含大小写不敏感匹配与关键字兜底）：Windows 雅黑 / macOS 苹方 /
  Linux 文泉驿·思源都能正确显示中文。

### 新增：服务器端设备管理

- **设备注册表**（`hub/src/zw_hub/devices.py`，SQLite）：记录机器码、主机名、平台、
  首次/最近在线、IP、状态。
- **踢下线**：置 `status=revoked`，之后该设备的心跳、注册、同步全部 `403 DEVICE_REVOKED`。
  **重连不能自愈**（否则踢下线没意义）—— 这是刻意的设计，有专门的回归测试。
- 踢下线只影响那一台；随时可恢复，下一次心跳（≤60 秒）生效。
- 改名、删除、事件留痕（谁注册/谁被踢/谁恢复）。

### 新增：Web 管理面板（`/panel`）

登录 `pscly` / `REDACTED_PANEL_PASSWORD`（可用 `ZW_PANEL_USER` / `ZW_PANEL_PASSWORD` 覆盖）。
设备列表（在线/离线/已踢三态）、KPI、事件流、踢下线/恢复/改名/删除。
会话 cookie 是带密钥的 HMAC（`httponly`、`samesite=lax`），不是用户名，伪造不了。

### 新增：发行包

- `packaging/zhiwen-client.spec`：PyInstaller onefile，Windows 上 `console=False`（双击不弹黑框）。
- `.github/workflows/release.yml`：推 tag 自动在 Windows/Linux/macOS 三平台构建并发布 Release，
  构建前先跑单元测试，构建后用 `--selftest` 验证产物不是坏文件。
- 踩过的两个打包坑（都已修，写进注释）：
  1. 入口用相对导入 → 打包后 `attempted relative import with no known parent package`；
  2. Tcl/Tk 运行库没收进去 → 换台机器就 `libtcl9.0.so not found`（uv/pyenv 的 Python
     把 tcl 放在自己的 lib 目录，只扫 `/usr/lib` 会漏）。

### 设计上的一个判断

「默认不要对接码」和「端口对局域网开放」看似矛盾。做法是：
**准入交给设备注册表** —— 每台电脑都要注册成设备，服务器上看得见、能踢；
令牌（`ZW_HUB_TOKEN`）降级为可选加固项，默认关闭。
想更严就设 `ZW_DEVICE_AUTO_APPROVE=false`（新设备要手动放行）或开令牌。

### 测试

142 → **192 项**。新增 `test_devices.py`（13）、`test_panel_api.py`（13）、`test_client.py`（24）。
文档一致性测试加入了 `DeviceError` 的扫描 —— 它立刻发现两个新错误码没进文档。

### 顺手修掉的

- `hub/docker-compose.yml` 里 `ZW_HUB_TOKEN` 用了 `${VAR:?}` 强制必填，
  导致「注释掉 .env 里的令牌」根本不生效（容器还带着旧值）。
- 面板首次加载用 `stats` 探活，会在 console 里留一条 401（看着像坏了），改用 `whoami`。
- hub 补 `/favicon.ico`，避免 404 污染 console。
- `normalize_server` 的边界：`http://`（只有协议）会被拼成 `http://http:8700` 这种假地址；
  大写 `HTTPS://` 不被识别。

---

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
| 单元测试 | 45 | **142** |
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
