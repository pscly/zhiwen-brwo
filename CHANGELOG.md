# CHANGELOG

## 0.7.19 — 2026-11-17

**把"商店链接装扩展"接成完整链路，并搬上界面。**

### 上一版缺的那一环

0.7.17 只把扩展代码放进了 `<数据目录>/extensions/<slug>-<version>/`——
**但没有任何环境会引用它**。`create_profile` 只按"默认包"给新环境挂扩展
（`apply_default_packs`），所以用户要的"以后新建浏览器默认就带上"
根本不会发生。代码在、没人用，等于没装。

现在 `install()` 默认顺手建包：

```
装扩展 → 入库 → 自动建扩展包 →（可选）设为默认 → 新建环境自动带上
```

实测（本地）：

```
装: {'slug': 'cookiemanager', 'version': '1.2.3',
     'pack_created': True, 'is_default': True}
默认包: ['cad89580d159']
新环境 extensions: ['cookiemanager']
```

- `make_pack=True`（默认）：装完建包；`as_default=True` 再标默认
- **建包失败不影响安装**：代码已经落好，返回 `pack_error` 让人手动建，
  不能因为"包没建成"把已经下载解包好的扩展也丢掉
- `make_pack=False` 留口子给"包我自己管"的场景

### 界面上终于有地方粘链接了

客户端「扩展包」面板新增三个按钮（原来只有 API，用户没入口）：

| 按钮 | 作用 |
|---|---|
| **商店链接安装** | 粘 `chromewebstore.google.com/detail/…` 或 32 位 ID |
| **选 .crx 文件** | 手动选一个 `.crx`/`.zip`（等价于"拖一个放上来"） |
| **检查更新** | 回商店比对已装扩展版本，列出 `当前→最新` |

> 为什么是"选文件"而不是拖拽：tkinter 原生不支持拖放，要做就得再引
> `tkinterdnd2` 依赖。选文件达到同样效果，少一个依赖。

无头 X 下验证过按钮真的存在且 handler 就位：
`['关闭','从环境导入','从 Chrome 目录导入','商店链接安装','选 .crx 文件','检查更新','刷新']`

### 测试

542 → **546 项**。新增：装完必须有包、`as_default` 能流进新环境
（端到端断言 `profiles.extensions` 含 slug）、`make_pack=False` 不建包、
建包失败不丢扩展。

## 0.7.18 — 2026-11-17

**CDP 令牌有效期 900 秒 → 3600 秒。**

15 分钟实测太短：一轮调试里要反复取令牌，经常"刚连上就过期"，
而且过期报错看起来像 CDP 或代理挂了，容易往错的方向排查。
改成 1 小时够跑完一轮调试，又不至于长期有效。

- `ZW_CDP_TOKEN_TTL` 默认值 900 → 3600（夹取范围仍是 60–7200，环境变量照样能改）
- 文档同步：部署指南配置表、接口文档示例与说明、排错手册
- 新增两条测试：默认值必须是 3600（防漂回去）、环境变量仍可覆盖

实测：真签发一个令牌，库里 `expires_at - issued_at = 3600` 秒。

## 0.7.17 — 2026-11-17

**输入 Chrome 商店链接就能装扩展**（新增需求）。

### 用户要求

> 扩展安装相关的，我希望也是可以直接输入谷歌的扩展链接，例如
> https://chromewebstore.google.com/detail/cookiemanager-cookie-edit/hdhngoamekjhmnpenphenpaiindoinpo?hl=zh-CN
> 就可添加，如果更新的话，他也可以自己更新，也可以自己手动拖动一个扩展放上去，他就自动帮我弄好

### 三种入口

| 场景 | 用法 |
|---|---|
| 粘商店链接 | `POST /api/v1/extensions/install {"url": "https://chromewebstore.google.com/detail/…/hdhng…"} ` |
| 粘 32 位 ID | 同上，`url` 直接填 ID |
| 手动拖一个文件 | `{"path": "C:\\Users\\…\\cookiemanager.crx"}`（也吃 `.zip`） |

链接解析接受 6 种形态（`?hl=zh-CN`、带/不带 slug、`?id=`、裸 ID）都测了。

### CRX 怎么解

商店给的是 **CRX3**，结构实测：

```
0    'Cr24'         4 字节魔数
4    version=3      uint32 小端
8    header_len     uint32 小端（protobuf 头，含签名）
12   header         header_len 字节
12+header_len  ZIP  ← 切掉前面就是要的东西
```

cookiemanager 实测：164717 字节，header_len=1310，ZIP 从 1322 开始（`PK\x03\x04`）。
CRX2 也支持（`pub_len`/`sig_len` 定位）。商店限流会回 HTML 页面 —— 那种情况
报"多半是商店限流或需要代理"，而不是烂在 zipfile 里。

### 为什么在客户端下载

服务器常常没有外网、也没有用户的代理；客户端才有。所以这个能力只在 core 侧，
hub 只负责保存打包结果。

### 更新

- 安装目录沿用 packs 的库布局 `<slug>-<version>`，版本升级不会覆盖旧目录
  （正在用旧版的环境不会被突然换掉）
- 装完写一个 `.zw-ext-id` 标记，`GET /api/v1/extensions/updates` 靠它回商店比对版本
- **手工导入的包没有 ID 标记 → 更新检查直接跳过**：宁可漏查，也不瞎猜 ID 去下载别人的扩展

### 安全

解包防路径穿越（`../`、绝对路径、盘符一律整体拒绝），有测试盯着。

### 测试

516 → **540 项**。新增 `tests/test_extstore.py`（24 项）：6 种链接形态、
CRX2/CRX3/纯 ZIP/HTML 错误页/截断/坏偏移、外层目录剥离、ID 标记与版本目录、
无 manifest 拒绝、路径穿越拒绝、幂等与 update 语义、无 ID 不查更新、
商店不可达的可读报错。

## 0.7.16 — 2026-11-17

**启动时那条代理提示说错了话，改掉。**（其余是云电脑上的整链路实测）

### 文案错误

启动带认证代理的环境时，返回的提示是：

> 该环境的代理带用户名密码，已通过内置认证扩展生效（不是命令行参数）

这是**假的**。Chrome 137+ 忽略 `--load-extension`（149/150/157 三种内核都实测过），
那个认证扩展根本没被加载。真正替浏览器补凭据的是**本机中继**。
用户照着这条去查扩展，只会白费时间。

现在改成事实描述，并带上实际的中继端口：

> 该环境的代理带用户名密码：浏览器连的是本机中继 127.0.0.1:51715，
> 由中继代填凭据后转发到真实代理（不是命令行参数，也不依赖浏览器扩展）

实现：`engines.build_cmdline` 在算出 `--proxy-server` 后，若是本机中继就把端口
回填到 `profile["_proxy_relay_port"]`；`manager.start` 据此生成提示。
（`build_cmdline` 收到的就是 `row` 本体，所以回填写得到。）

### 云电脑整链路实测（0.7.15 实机）

| 项 | 结果 |
|---|---|
| 坏代理启动 | 被拦，`PROXY_INVALID_HOST` + "多半是把 66.80.12.66 写成了 66.80.12 0.66" |
| 修好代理后启动 | `running`，命令行 `--proxy-server=http://127.0.0.1:51715`（走本机中继） |
| 中继是否在工作 | 51715 上有多个 `ESTABLISHED`/`TIME_WAIT` 连接，浏览器流量确实进了中继 |
| 检测页 | 启动后打开 `browserleaks.com/ip`、`ip123.in`、`creepjs` 三个 |
| 停止记住标签 | `start_urls` 被写成那三个页面 |
| 再次启动 | 恢复三页且**不重复**（去重生效） |
| 扩展探针（Chrome 157） | `extensions_active=false`，"实测：这个内核忽略 --load-extension（内容脚本没有执行）" |
| 代理上行（云电脑 → 3.5） | 自动出现在服务器库，`origin` 是设备号，密码加密 |
| 代理下行（3.5 → 云电脑） | `sync` 后 `added:1`，密码一并下发 |

### 测试

515 → **516 项**。新增：带认证代理时 `build_cmdline` 要把中继端口回填到 row
（这是那条正确文案的数据来源）。

## 0.7.15 — 2026-11-17

**修一个只在真实服务器上才会炸的依赖缺失。**

### 事故

0.7.14 的代理库在本地测试全绿，但部署到 3.5 后 `/v1/proxies` 直接 500：

```json
{"ok":false,"code":"INTERNAL","message":"ModuleNotFoundError: No module named 'nacl'"}
```

原因：代理库用 PyNaCl 做密码加密，而 **PyNaCl 是 core 的依赖，不是 hub 的**。
本地 venv 里因为装了 core 所以有，hub 镜像只装 hub 自己的依赖 → 那里没有。
本地测试因此完全测不出来。

### 修法

1. `hub/pyproject.toml` 加上 `pynacl>=1.5,<2`
2. `hub/Dockerfile` 的构建期依赖自检里加上 `nacl` ——
   缺依赖时**构建阶段就失败**，不要等容器起来在运行期 500
3. 新增测试 `test_hub_declares_every_third_party_import`：
   扫 hub 源码的所有第三方 `import`，逐个对 pyproject 的 `dependencies`。
   以后再有人"本地装了就用"，CI 会直接红。

> 顺手记一个写测试时的坑：第一版按 `dependencies = [` + `]` 手撕 toml，
> 被 `"uvicorn[standard]>=..."` 里的方括号截断，误报 pydantic 未声明。
> 改用 `tomllib` 正经解析。

### 真机验证（3.5，部署后）

```
GET  /v1/proxies                     → 200，空库
POST /v1/proxies（链式，scheme 填 socks5://）→ socks5://cf1b481103@66.80.12.66:20000 hop=http://127.0.0.1:21080
POST /v1/proxies/1/hide {hidden:true} → 默认列表 0 条，include_hidden=1 时 1 条
POST .../hide {hidden:false}          → 恢复显示
响应里 grep 密码                        → 0 次（明文不外泄）
```

### 测试

514 → **515 项**。

## 0.7.14 — 2026-11-17

**代理库变成服务器与用户机共享的一份**（新增需求）。

### 用户要求

> 代理可以在服务器里面进行添加管控，也可以在用户机上添加管控，都是一样的…
> 用户机上添加过的代理，都会自动同步到服务器里面，也可以在服务器里面删除，或者隐藏

### 数据流

| 方向 | 时机 | 行为 |
|---|---|---|
| 上行 | 导入代理 / 新增并绑定环境后，**后台线程**自动推 | 按 `scheme+host+port+username+hop` 去重，只 upsert 不删 |
| 下行 | core 启动自动；`POST /api/v1/proxies/sync` 手动 | 补本机没有的、更新标签；**不覆盖**本机改过的密码 |
| 删除 | 服务器上删 | 该条移除，用户机下次拉取不再出现 |
| 隐藏 | 服务器上隐藏 | 默认列表与设备拉取都不出现；**不删**，不影响在用它的环境 |

两个方向都**绝不阻断本地操作**：服务器连不上时只记日志 + 返回 `ok:false`。

### 接口

面板：`GET/POST /v1/proxies`、`POST /v1/proxies/{id}/hide`、`DELETE /v1/proxies/{id}`
设备：`POST /v1/devices/proxies`（上报）、`GET /v1/devices/proxies?with_secrets=1`（拉取）
本地：`POST/GET /api/v1/proxies/sync`

### 密码怎么存

"服务器加、用户机用"就必须让密码落到服务器上。为了不 naked 存储：

- `panel_secret` 经 **HKDF** 派生独立密钥（与会话签名在密码学上分开）
- **XSalsa20-Poly1305** 认证加密（PyNaCl）
- 库里只有 `password_enc`；**有测试直接扫 sqlite 文件确认无明文**
- 列表接口只给 `has_password`，明文只走设备专用接口
- 密钥变了明确报 `PROXY_SECRET_UNREADABLE`，不静默当空密码

### 控制台新增「代理」视图

表格显示上游/前置/标签/密码状态/来源，支持**添加、隐藏/取消隐藏、删除**。
和客户机共享同一份库，两侧改动互相同步。

### 修的两个坑

1. `scheme` 填 `http://` 会拼出 `http://://svr@host:port` → 新增 `norm_scheme()`
2. 代理视图的 JS 又被插进了 `<style>` 块（第二次踩同一个位置），
   表现为 `loadProxies is not defined`；已移进 `<script>`

### 测试

505 → **514 项**，新增 `tests/test_proxysync.py`（9 项，起真 uvicorn 的 hub）：
明文不外泄、密文落盘、密钥变更报错、隐藏三态、上行带 origin、下行带密码与链式前置、
重复拉取幂等、服务器不可达不抛异常、scheme 规范化。

## 0.7.13 — 2026-11-17

**"进程活着但本机服务死了"现在看得见了。**

### 为什么必须做

0.7.11 那次事故里最难查的一点：主控台显示设备**在线**、版本号也是新的，
但远程命令和远程控制台**全都连不上**。因为心跳和 core 是两条命：

- 心跳跑在客户端进程里，`local_summary()` 里原本写着 `if not st: return {}` ——
  core 不在就返回空，**心跳本身照样成功**
- 而 shell agent 和本机控制台端口都活在 **core 的 lifespan** 里，core 起不来就一起没有

结果：看的人完全不知道发生了什么，只能跑到机器前处理。

### 改了什么

客户端两条心跳路径都会上报三态字段：

```json
{"core_alive": false, "core_error": "端口 8730-8739 全被占用", "core_port": 8730}
```

- `gui.local_summary()`：core 不在时**也返回**（带上死因与端口），不再返回空
- `shellagent._payload()`：无客户端外壳时 core 就是本体，恒为 true

hub 侧：`devices` 表加 `core_alive INTEGER`（可空），`heartbeat` 用 `COALESCE` 写入，
`/v1/console/overview` 输出。**旧客户端不上报时是 `null`**，不能默认成"正常"。

控制台：设备卡新增红色徽章「本机服务未运行」，鼠标悬停说明常见原因
（端口被占 / 防火墙 / 杀软）与去那台机器上看哪里。

### 又踩了一次白名单

`normMachine` 是字段白名单，这次漏了 `core_alive` —— 表现是"徽章永远不出现"。
同一个坑这个项目已经踩过三次（`proxy`、`invalid/problem`、现在 `core_alive`），
所以顺手把 `realtime` / `shell_polling` 也一起补进白名单，并写了注释警告后来者。

### 测试

503 → **505 项**。新增：core_alive=false 能被 overview 读出、恢复上报后能翻回 true、
旧客户端不上报时是 null（不是 False/True）。

## 0.7.11 — 2026-11-17

**端口被占时自动回退 + 复用已跑实例**（云电脑上真踩到，代价是远程能力全失）。

### 事故经过

用户手动跑了一个便携版占了 8730。新装的客户端启动时 core 绑不上端口 →
`uvicorn` 在自己线程里抛错退出 → `LocalCore.start` 只能等满 45 秒再报"启动失败"。

后果比"起不来"严重得多：

- **心跳照常发**（`local_summary()` 里 `if not st: return {}`，core 不在就返回空，
  心跳本身不失败）→ 服务器上看着 `online=True`、版本号还是新的
- 但 **core 没起来 → shell agent 不轮询 → 远程命令失联；8730 不监听 → 远程控制台也失联**
- 结果：只能跑到那台机器前处理

### 修法

`client.LocalCore`：

1. **先探端口**（毫秒级 `bind` 测试），不再盲起等 45 秒超时
2. **已有健康实例就附上去**（`attached=True`）：用户开了两个窗口、或上次没退干净，
   第二个实例不会报失败，也不会把第一个的服务停掉（`stop()` 跳过 `attached`）
3. **端口被占就往后找 8731…8739**，用第一个能绑的，并记 `actual_port`
4. `is_running()` / `stats()` / 界面链接统一用 `live_port`
5. `ZW_PORT` 改成**赋值**而不是 `setdefault`（否则环境里残留的旧端口会把回退白做）
6. `manager.stats()` 补 `port` / `host` / `gateway_public_base` —— 回退后的端口必须
   报给服务器，否则 `core_base` 还是旧端口，远程照样连不上

### 测试

501 → **503 项**。新增：端口被占时回退到其它端口且 `stats().port` 与实际一致；
已有健康实例时 `attached=True` 且第二个实例 `stop()` 不误伤第一个。

## 0.7.10 — 2026-11-17

**扩展能不能加载：改成实测，而且结论和之前相反。**

### 纠正一个错误结论

0.7.6 我写进文档的"三种内核都不加载扩展"是**错的**。重新实测（同一台机器：

| 内核 | 版本 | `--load-extension` |
|---|---|---|
| `/usr/bin/google-chrome`（正式版） | 149.0.7827.53 | ❌ 不加载 |
| ungoogled-chromium | 150.0.7871.186 | ✅ **正常加载** |

版本都在 137 以上，结论却相反：官方 Chrome 关掉了这个能力，ungoogled 保留。

两个判据错误的教训（都记在 docs/16）：

1. **不能用版本号判断** —— ungoogled 150 明明可以，按"≥137"会被误判成不行。
2. **不能用 `/json/list` 里有没有 `chrome-extension://` 目标判断** ——
   ungoogled 上这个目标**不出现**（MV3 的 worker 不一定被列出），但扩展确实生效了。
   唯一可靠的信号是**内容脚本有没有在页面里留下痕迹**。

### 新增：`services/extprobe.py`（实测探针）

造一个只改 `document.title` 的一次性扩展 → 本地起一个 http 页面 →
无头启动内核带 `--load-extension` → CDP 导航读标题 → 看到标记就算能加载。

- 结果按（内核路径 + 大小 + mtime）缓存在 `extension-probe.json`，同一内核只探一次
- 正反都缓存：不支持那条要等满 10 秒超时，不缓存的话每次 apply 都卡 10 秒
- 探测跑不起来（文件不存在/起不来）时**按"不能加载"告警**，绝不静默放行

实测耗时：ungoogled 约 1.5s（能看到标记就返回），正式版 Chrome 约 10.7s（等超时）。

### 真实端到端验证（ungoogled 内核）

从 Chrome 目录导入 → 设默认 → 新建环境 → 启动浏览器 → CDP 读页面标记：

```
导入：RXYCDP 全套：1 个扩展，带数据 1 个
profiles.extensions = ['rxycdp-helper']
cmdline: --load-extension=/tmp/realpack/extensions/rxycdp-helper-2.1.0
页面标记 data-rxy = 'ACTIVE'      ← 插件真的跑起来了
```

### 测试

502 → 501 项（删掉 4 条基于版本号的旧用例，新增 4 条探针语义用例）。
关键断言：探针说不能就返回 False + 原因；探针抛异常按不加载处理；
内核不存在时立刻返回不卡住；缓存键含路径/大小/mtime（换版本会重探）。

## 0.7.6 — 2026-11-17

**扩展能不能真的被加载，现在说清楚了**（不再沉默）。

### 实测结论：Chrome 137+ 不加载命令行传入的扩展

三次独立实验，三种内核 × 有界面/无头，结论一致：

| 内核 | `--load-extension` 生效？ |
|---|---|
| google-chrome 149.0.7827.53 | ❌ |
| ungoogled-chromium 150.0.7871.186 | ❌ |
| playwright chromium | ❌ |

期望的扩展 ID（`chromium_unpacked_id` 算出的 `gkdidklgjlp…`）从未出现在
`chrome-extension://` 目标里，内容脚本从不执行。同时排除了三种"以为能绕"的做法：

- `--disable-features=DisableLoadExtensionCommandLineSwitch` —— 无效
- `--disable-extensions-except` 配合 `--load-extension` —— 无效
- 往 `Default/Preferences` 的 `extensions.settings` 注入
  （`location:4`/`state:1`/`creation_flags:38`/完整 `manifest`）——
  被 `Secure Preferences` 的 MAC 校验挡掉

这是 Chromium 上游从 137 起关掉的能力，不是本项目的缺陷。

### 因此：把"搬运"和"生效"分开说

`POST /profiles/{ref}/packs/apply` 新增两个字段：

```json
{"extensions_active": false,
 "extensions_note": "内核版本 Google Chrome 149.…（≥137）已移除命令行加载扩展的能力：…"}
```

- 代码（`extensions/<key>/`）、数据（`Local Extension Settings/<id>/`）、
  `profiles.extensions` 的 slug 照旧完整搬运 —— 换内核或换机器后仍然有用
- 但界面与事件流会明确提示"插件不会出现在浏览器里"，不再让人以为是自己操作错了
- 判断靠内核版本 ≥137（`engines.extensions_loadable()`），不猜厂商（ungoogled 同样失效）；
  版本读不出来时不拦

### 修的一个解析 bug

`engines._major_of()`：`google-chrome --version` 返回 `Google Chrome 149.0.7827.53`，
第一版按 `[.-]` 切后取第一个纯数字片段，取到的是 `"0"` → 主版本判成 0 →
"≥137 不支持扩展"永远不触发（正式版 Chrome 被报告成"可以加载扩展"）。
改成先用正则找出 `x.y.z…` 整体再取第一段。

### 文档

`docs/16-代理与扩展包.md` 新增第五节，写清限制、三次实验、接口字段，
以及三条真正能让插件生效的路线（CRX+企业策略 / 用 ≤136 内核 / 手工装一次）。

### 测试

493 → **500 项**。新增：版本字符串解析 6 种形态、现代内核判为不可加载、
旧内核放行、版本未知不拦、`apply` 返回 `extensions_active=false` 且代码仍被搬运。

## 0.7.5 — 2026-11-17

三件事：**代理串格式校验**、**从 Chrome 目录导入扩展包**、控制台能看到"代理无效"。

### 1. 代理填错不再静默失败

以前填错代理是**彻底静默**的：串被原样收下、存进库、显示在界面上，
浏览器起来没网络，任何地方都没提示。用户真实踩过：

```
socks5://cf1b481103:51ec63a321@66.80.12 0.66:20000
                                    ↑   ↑
                                 空格   IP 五段
```

现在解析时就拒掉，并说清错在哪（空格 / 段数 / 段值 / 非法字符 / IPv6 方括号 / 端口）：

- 新增 `_validate_host()`：主机名不许有空白；形似 IPv4 就必须正好四段且每段 0-255；
  域名只允许字母数字点连字符；含 `:` 的按 IPv6 校验（`[::1]` 支持）。
- 新增错误码 `PROXY_INVALID_HOST` / `PROXY_INVALID_PORT`。
- `urlsplit` 对 `[::zz]` 抛的 `ValueError` 也接住了 —— 否则是个 500。
- 三条入口都拦：导入（`failed` 里给脱敏原因）、绑定环境（`ok=false`）、
  启动（抛 `PROXY_INVALID_HOST`，不起一个没网的浏览器）。
- 错误信息里的密码永远是 `***`（有测试盯着）。

### 2. 从 Chrome 目录导入扩展包（RXYCDP 那条路）

`POST /api/v1/packs/import {"from_chrome": "C:/Users/你/AppData/Local/Google/Chrome/User Data"}`

RXYCDP / Roxy 这类浏览器**不是知纹启动的**，扩展不在知纹的 profiles 目录里，
而在标准 Chrome 的 `User Data/Default/Extensions/<id>/<ver>/`。现在给个路径就能
把代码 + `Local Extension Settings` 数据一起打包：

- 接受 `User Data`、`User Data/Default`、`Profile N`、`Default` 任一层
- 跳过 `_metadata` 之类的辅助目录
- 内部抽出 `_materialize_pack()`，与"从环境导出"共用同一段落盘/审计逻辑
- 客户端"扩展包"面板多了「从 Chrome 目录导入」按钮，默认值自动填好本机 Chrome 路径

### 3. 控制台显示「代理无效」

代理列现在有三态：正常（`socks5 host:port`）、**代理无效**（红色，点开给原因和改法）、直连。

修了两个同类的白名单漏字段问题：`normBrowser` 先后漏掉 `proxy` 和
`invalid`/`problem`，表现分别是"表头有列但永远直连"和"填错的代理显示成直连"。
这类"白名单静默丢字段"已经踩两次，字段都在注释里标了原因。

### 测试

483 → **493 项**。新增文件格式校验 6 组参数化用例、密码不泄漏、IPv6 与链式不被误伤、
`display_summary` 标记无效、坏代理阻断启动、`from_chrome` 五种场景。

## 0.7.4 — 2026-11-17

两件事：**链式代理**（前置 → 落地）和**控制台的代理列**。

### 1. 链式代理（用户：s5 只能做落地，要用代理去连它）

落地代理常常只能通过前置（机场/自建节点）访问，直连必然超时。
现在支持两跳：

```
http://127.0.0.1:21080 | socks5://user:pass@66.80.12.66:20000
                        ↑ 前置                ↑ 落地
```

- 分隔符 `|`、`>>`、`,` 都认；**左边前置、右边落地**
- 两段都支持单跳的全部写法（所以前置也能写成 `127.0.0.1:21080:user:pass`）
- 只支持两跳；写三段会明确报 `PROXY_CHAIN_TOO_LONG`
- `ProxySpec.key` 递归包含前置 —— 同落地不同前置会分配**不同**的中继端口（否则串号）
- 落地侧 DNS 由落地解析（`ATYP=0x03`），链式下同样成立

实现上把「连代理」拆成 `_connect_proxy`（只连 TCP）/ `_handshake`（在已连的
socket 上握手）/ `_open_tunnel`（两者组合）。链式就是：
先 `_open_tunnel(前置, 落地host, 落地port)`，再在**这条隧道上**对落地握手。
错误信息带 `前置代理：` / `落地代理：` 前缀，能一眼看出是哪一跳坏了。

> 分隔符只在 `|`/`>>` 失败后才试 `,` —— 否则 `http://u:pa,ss@h:8080` 这种
> 密码里带逗号的会被误切成链。

### 2. 控制台多了「代理」列

浏览器视图每行显示 `协议 host:port`，**点一下看详情**：协议/主机/端口/用户名/
有没有密码/完整地址（密码显示为 `***`）。

数据来自客户端心跳的 inventory：
- 客户端上报前就把密码换成 `***`
- hub 侧再按**白名单重建**一次（不盲信上报内容），万一某版把明文塞进来也不会入库
- 详情弹窗里写明「改代理要回客户端」——代理是浏览器**启动期**参数，服务器改不了

### 3. 顺手修的

- `normBrowser` 的白名单漏了 `proxy` 字段 → 表头有代理列但永远显示「直连」
- `proxyCell` 用了不存在的 `escAttr`（本文件只有 `esc`）
- 控制台 `openDialog` 新增 `html` / `hideCancel`，纯展示弹窗不再显示「取消/确定」
- 无头模式不接受命令行传多个网址（`Multiple targets are not supported in headless mode`
  会导致浏览器启动即退出）：改为只传第一个，其余在就绪后用 CDP 补开

### 测试

461 → **478 项**。新增 `tests/test_proxy_chain.py`（15 项，真 socket 串两个假上游）
与代理列相关 2 项（含"hub 不能存下代理密码"的回归）。

## 0.7.0 — 2026-11-17

四件大事：**实时终端**、**代理（含带认证）真正可用**、**客户端控制台免令牌**、**扩展包**。

### 1. 实时终端（WebSocket）

原来是"一问一答"：命令跑完才有输出。现在设备与 hub 之间保持一条持久 WebSocket，
浏览器/CLI 接上去就能**边跑边出字**：

- 持久 shell 会话：`cd`、环境变量跨命令保持；`exit` 后自动重开
- 流式输出：`ping -n 10`、`gradle build` 这种终于能看到过程
- 支持交互式输入（`python`、`node` 能给 stdin）
- 连不上时**自动回退**到原来的队列模式，徽章显示「实时」/「队列」

实测：投递到出字 <1s；`cd /tmp` 后再 `pwd` 得到 `/tmp`；
跨机（3.5 hub ↔ 3.6 core）同样通过。

> 明确限制：**不是真 PTY**。`vim`/`htop` 这类需要终端控制的程序不正常，
> 请用 SSH 或远程桌面。

### 2. 代理：带认证的终于能用了

**实测确认 Chrome 149 已忽略 `--load-extension`** —— 原来"生成认证扩展去应答
`onAuthRequired`"的方案在新内核上**静默失效**（连不上且无报错）。

改成**本机中继**：`--proxy-server=http://127.0.0.1:<port>`（无认证，引擎原生支持），
中继替浏览器补 `Proxy-Authorization` 或做 SOCKS5 用户名密码握手。
引擎无关、可观测、可测。SOCKS5 发**域名**让 DNS 也走代理（否则 DNS 泄漏）。

已用真 Chrome 149 端到端验证：中继 → 需 Basic 认证的 HTTP 上游 → `example.com` 加载成功；
负向对照（Chrome 直连同一上游）空白页。SOCKS5 + RFC1929 同样通过。

配套：
- 批量导入：一行一个，支持 `socks5://u:p@h:port`、`http://…`、`h:port:u:p`、
  `u:p@h:port`、`h:port`、行尾 `# 备注`；密码永不回显
- 单个/批量出口检测（socks5 检测依赖 `httpx[socks]`，已写进依赖）
- **创建后随时换代理**：`PUT /profiles/{ref}/proxy`；运行中的环境返回
  `restart_required`（Chromium 的 proxy 是启动期参数，不热改）
- 修 `proxy_spec_for` 凭据未转义：密码含 `/` `#` `@` 时 URL 拼坏 → 静默连不上

### 3. 客户端控制台：本机免令牌，远程用机器码

- 本机打开 `http://127.0.0.1:8730/`：**不需要任何密码**
- 远程打开：输入**这台电脑的机器码**（`zwd_…`）
- 默认监听 `0.0.0.0:8730`（`ZW_HOST`/`ZW_PORT` 可改）
- 对外监听但没有凭据时**自动降级回回环**并记日志（不再抛异常，否则 import 就崩）

### 4. 扩展包 + 启动默认检测页

- **扩展包**：把某个环境里的"扩展代码 + `Local Extension Settings` 数据"打包，
  可设默认、新建环境自动带上。这就是"把 RXYCDP 的插件和数据一起打包"的做法。
  不搬 `Local Storage/leveldb`（混着所有源）与 `Sync Extension Settings`（云端权威）。
- **启动页**：先恢复上次关闭时的标签（关浏览器前用 CDP 抓一次），再追加检测页。
  实测可用：`browserleaks.com/ip`、`ip123.in`（中文 IP 纯净度）、`creepjs`（综合指纹）。
  剔掉了 ping0.cc（验证墙）、scamalytics（查询表单）、ip.cn、whoer.net。

### 5. 客户端 GUI

- 新建环境可填代理并**测试**（立刻看出口 IP / 错误）
- 环境行「代理」按钮：看/改/清除/测试，运行中提示重启生效
- 头部：批量导入代理、代理库（含批量检测）、扩展包面板（从环境导出/设默认/应用）

### 测试

342 → **457 项**。新增：会话引擎与中继 11 项、代理库 62 项、中继代理 16 项、
扩展包与启动页 18 项、新路由与鉴权 8 项。

## 0.6.0 — 2026-11-17

三件事：**新设备要你点一下才放行**、**远程终端变得可诊断**、**客户端不再闪黑框**。

### 1. 授权流程：装好 → 等授权 → 你点允许 → 客户端自动继续

原来 `ZW_DEVICE_AUTO_APPROVE` 默认 `true`，任何客户端填个 IP 就能用。
现在默认 **false**：

```
客户端装好 → 显示「等待服务器授权」
           → 主控台出现一台「待授权」的机器（KPI 有待授权计数）
           → 你点「允许」→ 客户端 3 秒内自动转「已连接」，不用再操作
           → 点「拒绝」→ 同步被拒，记录保留，随时能改回允许
```

**未授权也能用远程终端** —— 这条是刻意的：授权管的是"能不能同步数据"，
不是"能不能被管理员操作"。新机器一上线你就能先上去跑命令看环境，再决定放不放行。

实现上分了两个校验：
- `require_active`：数据面（`/v1/*` 同步、租约）必须已授权 → `403 DEVICE_PENDING`
- `require_known`：心跳、注册、**远程命令**只要求"登记过且没被踢/拒绝"

想回到老行为：`.env` 里设 `ZW_DEVICE_AUTO_APPROVE=true`。

### 2. 远程终端"下发没反应"现在能自己看出来

用户反馈"下发指令等半天没反应"。根因在**旧版客户端（0.4.1）没有在拉命令**，
但当时界面上完全看不出区别。现在：

- hub 记录每台设备**最近一次来拉命令的时间**，主控台卡片上给两个徽章：
  **终端就绪** / **终端未连接**（鼠标悬停有原因说明）
- `/v1/console/overview` 增加 `shell_polling` 与 `shell_last_poll_ago`，
  AI 也能据此判断"是设备没在拉"还是"命令本身有问题"
- 客户端把界面事件日志**同时写一份到 stdout**（从命令行启动或排障时能看到完整时间线）

> 实测（0.6.0 客户端 + 3.5 hub）：投递到执行 **< 1 秒**。

### 3. 客户端不再闪 CMD 黑框

客户端是 GUI 子系统，但它会拉起一堆控制台子进程（`tasklist` / `taskkill` /
`netstat` / `powershell` / `icacls` / 远程命令用的 `cmd`）。Windows 上从 GUI 进程
拉起控制台程序、没指定 `CREATE_NO_WINDOW` 就会闪黑框。

新增 `core/src/zw_core/services/proc.py` 的 `run_hidden` / `popen_hidden`：

- 自动加 `CREATE_NO_WINDOW`（0x08000000），调用方已有 `creationflags` 时**位或**合并
- 同时设 `STARTF_USESHOWWINDOW` + `SW_HIDE` 双保险
- POSIX 原样透传，不塞任何平台参数

把 `core/src` 下 **14 个**调用点全部换过去（engines 10、shell 2、leases 1、client 1）。
新增审计测试：`core/src` 里除 `proc.py` 外**不允许**出现裸的 `subprocess.run/Popen`
（留了带原因的 `# proc-exempt:` 豁免口子，目前零豁免）。

**一个例外**：Chrome 主进程用 `popen_hidden(..., gui_child=True)` 但**不设** `SW_HIDE` ——
`wShowWindow` 会被 GUI 程序当成 `WinMain` 的 `nCmdShow`，设成隐藏等于启动一个看不见的浏览器。

### 4. 客户端能自己管浏览器了

- **环境 tab**：列出本机所有环境（名称/状态/分组），可**新建、启动、停止、删除**；
  新建用自绘浮层（不用原生 `simpledialog`），重名会留在浮层里让你改，不用重开
- **日志 tab**：本机事件流（可只看错误）、打开日志目录、一键复制诊断信息
- 5 秒自动刷新，但**你正在输入或操作时整轮跳过**，且渲染签名没变就不重建控件
- 窗口 640×720，小屏（1366×768）实测无控件被裁

### 测试

313 → **341 项**。新增：
- `tests/test_approval_flow.py`（9）：待授权是默认、待授权能心跳、**待授权不能同步**、
  **待授权能用远程终端**、允许后转 active、拒绝后终端仍可用且可再允许、KPI 有 pending、
  `shell_polling` 从 false 变 true、`auto_approve=true` 仍有效
- `tests/test_proc_hidden.py`（19）：Windows 标志位与合并、POSIX 不加参数、参数透传、
  `check=True` 语义、以及"源码里不许有裸 subprocess"的审计

## 0.5.0 — 2026-11-16

把 `http://<服务器>:8700/` 做成**真正的主控台**：打开就是登录页，进去能看到
机器、浏览器、CDP 地址、令牌，AI 用同一批接口就能知道有什么、并去控制。

### 之前是什么样

`/` 返回 `{"detail":"Not Found"}`，面板藏在 `/panel`，只能看设备列表与事件，
看不到机器上有什么浏览器，更看不到 CDP 地址。用户原话："现在的几乎是个
完全不可行的半成品"。

### 现在

- **`/` = 主控台**，`/panel` 永久 302 到 `/`（老书签不断）
- 四个视图：**机器 / 浏览器 / 令牌 / 事件**，`#hash` 记忆，刷新停在原视图
- **机器**：KPI（机器/在线/浏览器/运行中）+ 服务器信息条 + 卡片；
  卡片上能开远程终端、打开该机的控制台、踢下线/恢复、改名、复制机器码
- **浏览器**：按机器分组列出每个环境、状态、分组、**CDP HTTP / WS 地址**（可点可复制）
- **令牌**：面板账号/密码、hub 令牌、网段白名单、自动放行，支持显示/隐藏与复制
- **事件**：注册/踢下线/恢复/删除/改名，带操作者

### 设备怎么把浏览器清单报上来

心跳里新增两个字段（都可选）：

```json
{"core_base": "http://192.168.3.6:8730",
 "inventory": [{"id":"...","name":"环境A","state":"running","group":"默认","cdp_path":"/cdp/..."}]}
```

- **只报元信息**，不带 cookie / 密码 / 指纹种子
- hub 侧做基本清洗（字段裁长度、单机上限 200 条），空心跳**不会**清空已上报的清单
- `core_base` 可达性由 hub 用 300ms 超时**尽力探测**：NAT 后面的机器必然探测不到，
  这是 `false` 而不是错误，界面标「控制台不可直连」并提示改用远程终端

### AI 怎么用

`GET /v1/ai/manifest` 现在包含：

- `machine` / `browser` 概念说明
- workflow「先看有什么」→ `GET /v1/console/overview`
- workflow「接管某个浏览器」→ 取 `cdp_ws` 用 `connectOverCDP`；不可达就走远程终端

### 顺手修的

| 问题 | 后果 |
|---|---|
| `HEAD /` 返回 **405** | 3.5 的服务导航页（u.pscly.cn）用 `HEAD /__probe/http/8700` 判断在线，405 会让状态点**永远灰着**，看起来像服务挂了。补了显式 HEAD 路由 |
| 无客户端的 core 不会注册/心跳 | 服务器上直接跑的 core 注册不了、也 3 分钟被判离线。现在 core 启动时自己注册 + 独立心跳（**仅当配了 `ZW_DEVICE_ID`**；服务器端本身不该是设备） |
| 切换"允许远程命令行"要等最多 60 秒 | 面板看不到开关变化。现在切换后**立刻**发一次心跳 |

### 入口

- 3.5 的服务导航页（`u.pscly.cn`，`/root/docker/nginx1/html1/index.html`）
  第一个卡片就是「知纹 · 机群主控台 → 8700」，带在线状态点（已实测 `up`）

### 测试

292 → **309 项**。新增 `tests/test_console_api.py`（12 项）：鉴权、
机器与浏览器聚合、**空心跳不清空清单**、不可达探测是 false、无 core_base 是 null、
清单清洗（限条数/裁长度）、`secrets` 未鉴权 401、Bearer 可调、HEAD `/` 200、
`/panel` 302。

页面侧另有 `scripts/console-ui-check.cjs`（51 条断言，真浏览器 + mock 后端），
覆盖登录、四个视图、终端保留输入与滚动、令牌显示/隐藏、窄屏、接口 404 兼容。

## 0.4.1 — 2026-11-16

把**安装路径全部改成纯英文**。中文只保留在显示名与界面文案里。

### 问题

安装后目录是 `C:\Program Files\知纹客户端` —— 路径里有中文。这在很多地方会咬人：
旧版命令行工具不解 UTF-8 路径、备份脚本按 GBK 处理会乱码、日志里带中文路径不好 grep、
跨机复制容易出错。

### 改法：显示名与路径名彻底分开

| 用途 | 值 |
|---|---|
| 显示名（添加/删除程序、快捷方式注释） | `知纹客户端`（保持中文，用户看着舒服） |
| **安装目录 / 开始菜单组 / 快捷方式文件名** | `ZhiwenClient`（纯 ASCII） |
| 可执行文件 | `zhiwen-client.exe` |
| macOS 的 `.app` 文件名 | `ZhiwenClient.app`（中文名放 `CFBundleDisplayName`，Finder 里仍显示「知纹客户端」） |
| 配置目录 / 数据目录 / 自启项 | `zhiwen-client` / `zhiwen` / `ZhiwenClient`（本来就是英文，这次加了测试钉住） |

装完的实际路径：

- 管理员安装 → `C:\Program Files\ZhiwenClient`
- 普通账号（默认 `PrivilegesRequired=lowest`）→ `%LOCALAPPDATA%\Programs\ZhiwenClient`

### 防止以后又变回去

- `check-iss.py` 新增 **C36**：扫描 `DefaultDirName` / `DefaultGroupName` /
  `[Icons] Name` / `[Files] DestDir|DestName` / `[Dirs]` / `[UninstallDelete]`，
  任何一条含非 ASCII 字符就判失败（做过负向验证：把目录改回中文，C09 与 C36 同时报 FAIL）。
- 新增 `tests/test_ascii_paths.py`（9 项）：安装脚本、`.app` 名、配置目录、
  数据目录、自启产物名，以及"显示名**允许**中文"这条反向断言。
- 客户端自检新增一项「安装路径为纯英文」：路径含中文时**只告警不判失败** ——
  因为 Windows 用户名本身是中文时 `%LOCALAPPDATA%` 那一段用户改不了，
  判死只会让人以为程序坏了。

### 最容易漏的一点：升级会沿用旧目录

只改 `DefaultDirName` 对**升级用户完全无效**。Inno 默认
`UsePreviousAppDir=yes` —— 它从注册表读"上次装在哪"，然后原样装回去。
所以你从 0.4.0（中文目录）升上来，路径仍然是中文，看起来像修复没生效。

现在显式 `UsePreviousAppDir=no` / `UsePreviousGroup=no`，
并在 `[InstallDelete]` 里清掉旧的中文目录、旧开始菜单组、旧桌面快捷方式 ——
否则会留一份孤儿文件（卸载项指向新目录，旧文件再也删不掉）。

检查器为此加了 **C37**，负向验证过：删掉 `UsePreviousAppDir=no` 立刻报 FAIL。

### 一个说明

如果 Windows 用户名是中文，"普通账号安装"的路径里仍会带中文
（`C:\Users\张三\AppData\Local\Programs\ZhiwenClient`，那一段是系统账户名，
安装程序改不了）。这种情况要么用管理员权限装到 `Program Files`，
要么在安装界面把目录改成 `D:\ZhiwenClient` 这类纯英文路径。自检会提示这一点。

### 测试

280 → **292 项**（`test_ascii_paths.py` 12 项）。

## 0.4.0 — 2026-11-16

新增**远程命令行**：服务器/网页可以让某台电脑执行命令。默认开启，客户端可关。

### 为什么是"设备主动拉"而不是"服务器连进去"

客户端可能跑在云电脑/NAT 后面，服务器连不进去它的 127.0.0.1。
所以命令走**队列 + 长轮询**：设备主动向 hub 要命令，执行完回传结果。

**设备不新开任何监听端口** —— 这是这个设计最重要的安全属性。

### 能力

- 面板里每台设备多一个「终端」：输入命令、看输出（stdout/stderr 分色）、
  退出码、耗时；`↑/↓` 翻历史；离线也能投递，上线后执行。
- 接口：`POST /v1/devices/{id}/shell` 投递，`GET .../{job}?wait=30` 等结果，
  `GET .../history` 查历史，`POST .../{job}/cancel` 取消未下发的。
- 本机：`zw shell "命令"`、`zw shell-status`；HTTP `/api/v1/shell/{status,exec,enable}`。
- 心跳上报 `shell_enabled`，面板据此显示「命令行：开/关」并决定输入框是否可写。

### 关闭是怎么生效的（两道闸门）

1. 开关关掉 → 客户端**根本不向 hub 拉命令**（不是"拉了不执行"）
2. 极端时序：命令已拉到本地、执行前一刻被关掉 → 也不执行，回 `SHELL_DISABLED`

只做第一道的话，用户看到命令还是被跑了一次，那是很吓人的体验。

### 执行器的几个刻意设计

- **超时按进程组杀**：`taskkill /T /F`、`killpg`。只杀父进程会留下 `ping -t`
  这类子进程继续占端口。
- **输出按 UTF-8 字符边界截断**：直接 `[:N]` 会把汉字切成半个，前端显示乱码。
- **stdout/stderr 分开收**：合并会让错误信息淹在正常输出里，面板也没法分色。
- **默认工作目录是用户主目录**，不是 `/` 或 `C:\`。
- **审计日志记命令但不记输出**（输出里可能有密码）。
- **多编码兜底**：Windows 的 `ipconfig` 输出是 GBK，不能只按 UTF-8 解。

### 测试

233 → **279 项**。新增：
- `tests/test_shell.py`（14）：stdout/stderr 分离、非 0 退出码、超时杀进程、
  中文不乱码、超长截断不切汉字、空命令/超长命令/坏 cwd 的拒绝、
  默认目录不是根、**日志里不出现命令输出**
- `tests/test_shellagent.py`（8）：拉取→执行→回传全流程、关闭后零请求、
  拿到命令后被关掉也不执行、被踢后不重试风暴、网络错误退避、换 hub 重指向
- `tests/test_hub_shell.py`（19）：队列/长轮询/队列满/超长命令/设备互不可见/
  取消语义/截断仍是合法 UTF-8/心跳落库
- `test_api_smoke.py` 新增 5 项：status/exec/enable、本地接口不受远程开关影响

### 验证方式

不只跑单测，还做了真链路：本机起真 hub + 真设备代理，用浏览器在面板里
点「终端」执行 `echo 面板真机测试 && uname -s`，拿到 `面板真机测试` + `Linux`、
退出码 0、耗时 2ms，console 零报错。

## 0.3.3 — 2026-11-16

安全收口：**仓库里不再有任何真实凭据**。

### 默认凭据改为 `admin` / `1234`

原来的默认值是开发机上用的真实账号密码，而且写进了代码、文档、发布说明里。
仓库是公开的，等于把面板口令公开。

现在默认是 `admin` / `1234`，并且：
- 仍在用默认值时，面板顶部会挂一条橙色提醒条（`meta.default_credentials`）
- 部署脚本新建 `.env` 时**不再写死密码**，改为随机生成并打印出来
- 测试用 `testadmin` / `test-panel-pass`，不再使用任何真实凭据
- 文档与发布说明只写默认值和改法，不写任何真实值

### 面板登录也走网段白名单

`ZW_ALLOWED_CIDRS` 原来只作用于 `/v1/*`，面板登录不受约束 ——
面板密码是给人记的短口令，没有网段限制时一旦泄漏就等于把面板交出去。
现在 `/panel/api/login` 与已登录的面板接口都会校验：
配了名单就只放行名单内来源，其余一律 `403 CIDR_FORBIDDEN`。
默认不配（不改变现有行为），按需开启。

### 历史清理

代码里的默认值改掉还不够 —— 历史提交里仍然有那个字符串。
已用 `filter-branch` 重写全部历史（文件内容 + 提交信息），
`main` 与三个 tag 都已强制更新。全对象扫描确认无残留。

> 事实说明：GitHub 会缓存已不可达的提交对象一段时间，
> 旧 SHA 仍可能被直接访问或被代码搜索命中。彻底的消除需要
> 重建仓库或联系 GitHub 支持。实际影响有限（面板只在局域网内可用），
> 但要知道这件事，别以为改了当前文件就完事。

### 测试

230 → **233 项**（+2 面板网段校验，+1 默认凭据标记）。

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

### 修：Windows 上启动要卡满 20 秒

CI 的 Windows job 把它抓出来了：日志里从「知纹核心启动」到
「Application startup complete」**恰好 20 秒** —— 那正是某个子进程的超时值。

根因是 `engine_version()` 用 `chrome --version` 探版本：
Linux/macOS 的 Chrome 会把版本打到 stdout 然后退出，**Windows 的
chrome.exe 忽略 `--version`**，它照常启动浏览器（开窗口、常驻进程），
stdout 一直是空的 → `subprocess.run(timeout=20)` 一路等到超时。

后果：Windows 上每次启动客户端都要白等 20 秒，看起来像"服务起不来"。

修法：
1. 先从**安装路径**里读版本（Chromium 发行包通常把版本放在目录名里，
   如 `ungoogled-chromium-150.0.7871.186`、`chromium-1208`）—— 零成本、不启动进程；
2. Windows 上只用 PowerShell 读 PE 的文件版本信息，绝不传 `--version`；
3. 其它平台才用 `--version`，超时从 20 秒压到 5 秒。

`LocalCore.start` 的就绪等待也从 20 秒放宽到 45 秒（Windows 冷启动要过杀软扫描），
并且超时后不再是干巴巴一句"没起来"，而是列出三个最常见原因 + 日志路径。

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

222 → **230 项**。新增 `tests/test_no_console.py`（4 项）：
无流时 `setup_logging` 不崩且仍写文件、`LocalCore` 确实传了 `log_config=None`、
无流时能真启动并响应 `/healthz`、无流时统计接口可用。

新增 `tests/test_engines_extra.py` 里 4 项盯住版本探测：
路径能读出就不许起子进程、Windows 分支不许出现 `--version`、
POSIX 超时必须 ≤5 秒。

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

登录凭据由 `ZW_PANEL_USER` / `ZW_PANEL_PASSWORD` 决定（默认 `admin` / `1234`，上线前必改）。
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
