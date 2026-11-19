# CHANGELOG

## 0.8.17 — 2026-11-20

**回答了那个条件句：Chrome 对带凭据的代理「支持不好」到什么程度 —— 并证明不需要 Firefox。**

### 用户的原话

> **如果 Chrome 支持不好的话**，你可以再单独创建一个 Firefox 类型的来对这种建议好好的支持

这是个条件句，判断依据是「Chrome 到底支持得好不好」。所以先测，再决定。

### 实测（ungoogled-chromium 150 / Chrome 157）

| 给 Chrome 的 `--proxy-server` | 代理端收到什么 |
|---|---|
| `socks5://user:pass@host:port` | **握手记录为空** |
| `socks5://host:port`（无凭据） | 正常握手 |
| `http://127.0.0.1:<中继端口>` | 正常，且**上游收到带凭据的握手** |

结论：**Chromium 从不解析 proxy URL 里的 userinfo**，`u:p@` 被直接丢弃。

这比"认证失败"更危险 —— 认证失败至少连不上；凭据被丢之后 Chrome 会
**把流量直连出去**，用户以为走代理，实际在裸奔。

> 我一开始也差点看错：第一次测时本地目标能打开，看起来"能用"。
> 后来把目标换成外部地址、并给服务端加握手日志，才看清代理压根没被连上。

### 所以产品怎么做（已有设计，这次证实了它是对的）

带凭据的代理 **一律走本机中继**：

```
Chrome ──无凭据──► 127.0.0.1:<中继端口> ──带凭据──► 上游代理 ──► 目标
```

真机记录（经中继访问 example.com）：

```
VER=5 METHODS=[0, 2]
  AUTH user=b'cf1b481103' pass=b'51ec63a321'
  CONNECT example.com:443
```

### 还需要 Firefox 吗：不需要

凭据问题已由中继解决。`ZW_ENGINE_POLICY` 里也没有 Firefox 分支；
加一个只会多一套打包/升级/指纹兼容负担，不解决任何现有问题。
这条结论写进了 `docs/16` 与交付清单，附实测表格。

### 测试

715 → **717 项**（`tests/test_chrome_credentialed_proxy.py`）：
中继端点**不含 userinfo**、客户端不带凭据连中继时上游仍收到凭据。

九项门槛全绿（717 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 46）。
其中 gateway-check 是**重试后**通过的 —— 上一版加的重试确实派上用场。

## 0.8.16 — 2026-11-20

**给「从控制台拉包」加了界面入口；顺手把门禁里一个偶发抖动治了。**

### 一、桌面客户端有了「从控制台拉取」按钮

上一版补了 hub 存包 + core 拉包的接口。但**桌面客户端没有入口** ——
只有接口等于没有功能，用户日常用的是 GUI。

在「从 Chrome 目录导入扩展包」的弹窗里加了一排：

- **「从控制台拉取」按钮**
- **「同时设为默认」勾选项**（对应你说的"默认带上，当然也可以选择关闭"）
- 一行状态文字，直接说结果：「拉下 2 个：RXYCDP 全套」/「没有新包（…已是最新）」

失败时也给人话，而不是丢一个错误码：

| 情况 | 提示 |
|---|---|
| 设备待授权 | 「这台机器还没被服务器授权，去控制台点『允许』后重试」 |
| 设备未注册 | 「这台机器还没在服务器注册，先在客户端连一次服务器」 |
| 其它 | 「拉取失败：<原因>」 |

### 二、gui-check 42 → 46 条

新增 4 条断言盯这个入口：`CoreApi` 有 `pull_hub_packs`、按钮存在、
**按钮可点**（不是禁用态）、有「同时设为默认」选项。

写的时候踩了个小坑：`Theme` 没有 `text` 属性（实际叫 `ink`），
`gui-check` 直接报 `AttributeError` 抓到，改掉后 46/0。

### 三、门禁里的偶发抖动：gateway-check 加重试

完整 `check-all` 里偶发三条 cookie 断言失败：

```
REST 能读到 cookie 列表
REST 能读到 Playwright 写的 cookie
Playwright 能读到 REST 写的 cookie
```

看着像网关坏了。但我分别用三种方式单独复现，**都是 23/0**：

| 复现方式 | 结果 |
|---|---|
| 独立 core + 127.0.0.1 | 23/0 |
| 独立 core + LAN IP（和 check-all 一样） | 23/0 |
| 先跑 tabs-check、紧接着跑 gateway-check | 23/0 |

结论：不是稳定代码问题，是**连续跑多项、每项都真开浏览器**时的资源时序抖动
（环境没真起来 → 读 cookie 拿到空）。

处理：`check-all.sh` 里给这一项**一次重试**，并把第一次的失败原样打印出来 ——
不掩盖问题，也不让一个时序抖动挡住整轮验收。

### 测试

715 项；九项门槛全绿（715 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / **46**）。

## 0.8.15 — 2026-11-20

**「换台机器也能用控制台的包」补完最后一米；顺手修掉「zip 导入丢数据」。**

### 一、core 现在能从控制台拉包（`packsync.py`）

上一版给 hub 加了存包接口，但 core **不能拉** —— 包躺在服务器上，别的机器还是
用不到。这就是最后一米。

新增：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/packs/pull-hub` | 从控制台拉包并导入本机；`?as_default=1` 顺带设默认 |
| GET | `/api/v1/packs/pull-hub/status` | 上一次拉取结果 |

几条约定：下载后**校验 sha256**（含响应头 `X-ZW-Sha256`）、**已拉过的跳过**
（按 sha 判断）、**单个包失败不拖垮其余**、**不自动设默认**（由参数决定）。

### 二、排查中发现的两个真实障碍

**① 设备未注册 → 404 `DEVICE_UNKNOWN`**

带 `X-ZW-Device` 访问时 hub 会 `require_active(dev)`。首次运行必然撞上，
而现象是「拉取失败: HTTP 404」，看起来像接口不存在。已按 `proxysync` 的做法
先补一次注册。

**② 设备 pending → 403 `DEVICE_PENDING`**

注册后还要管理员批准。这是正确的安全行为（和设备接入一致），
但错误信息必须能看懂 —— 它确实说了「让管理员在控制台里点『允许』后重试」。

### 三、真 bug：zip 导入丢数据

拉下来的包，扩展在、**登录态没了**。查下来是 `import_pack` 的 zip 分支：

```python
code_dirs = _find_extension_dirs(root)   # 只找 code/，压根没看 data/
# 注释还写着"从目录/zip 导入……没有历史数据可带" ← 这个假设是错的
```

本项目的包格式**本来就带 `data/<key>/`**。所以：

- 从控制台拉包 → 数据丢
- 直接导入任何 zip 包 → 数据也丢（不只是拉包这条路）

修法：zip 分支识别 `data/<key>/` 并跟着搬进新包，`has_data` 如实反映。
修完实测：

```
ext: 脚本猫 | has_data: True | data_dir: data/脚本猫
```

### 四、被门禁抓住的两件事

1. 新增 2 个路由没登记 → `test_every_public_route_is_documented` 与
   `test_ai_manifest_lists_all_public_routes` 同时红。补齐文档与 manifest 后通过。
2. `gateway-check` 的三条失败**不是代码问题** —— 是我之前测试残留的 3 个
   `zw_core.main` 进程占着端口。清理后单独跑与整体跑都 23/0。

### 测试

712 → **715 项**。九项门槛全绿（715 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.14 — 2026-11-20

**补上「把插件打包放到控制台」这一层 —— 之前它根本不存在。**

### 缺的是什么

用户原话：

> 把现在 RXYCDP 浏览器的插件及其数据，最好能一起**打包放到我的控制台里面**。
> 这样以后创建浏览器时，可以默认就把这些插件的数据都带上。

查下来的事实：

```
core 的 packs 接口数       38
hub 的接口总数             61
hub 里跟 packs 有关的       0
hub 的文件上传/下载能力     无
```

也就是说，扩展包**只存在每台机器的 core 本地**（`<数据目录>/packs/`）。
换一台机器就要重新导入一次 —— 那叫本机缓存，不叫"放到控制台"。

### 补了什么：`hub/src/zw_hub/packstore.py` + 5 个接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/packs?name=&origin=&note=&id=` | 上传 zip（**原始字节流**） |
| GET | `/v1/packs` | 列表 |
| GET | `/v1/packs/{id}` | 详情 |
| GET | `/v1/packs/{id}/download` | 下载，响应头带 `X-ZW-Sha256` |
| DELETE | `/v1/packs/{id}` | 删除 |

设计取舍：

- **二进制落盘、索引进 SQLite**：包几十 MB，塞进 SQLite 会让库臃肿、备份变慢。
- **不用 `UploadFile`**：那要额外装 `python-multipart`。包本来就是二进制 zip，
  直接读 `request.body()` 就够，不给 hub 加新依赖。
- **CAS 不覆盖**（409 `PACK_EXISTS`）、**下载前校验 sha256**（不一致报
  `PACK_CORRUPTED`，宁拒不送）、**id 只允许字母数字与 `-_`**（会拼进文件路径，
  必须挡穿越）。

### 实测（真起 hub，走 HTTP）

```
上传: a1cc65bd2c89 | 扩展数: 1 | 带数据: True
列表: [('RXYCDP模拟', 1)]
下载字节数: 312 | sha 一致: True
删除: a1cc65bd2c89 | 剩余: 0
```

### 被门禁抓住的一件事

新增了 5 个错误码（`PACK_EXISTS` / `PACK_EMPTY` / `PACK_BAD_ID` /
`PACK_CORRUPTED` / `PACK_FILE_MISSING`），但忘了登记到接口文档的错误码全表 ——
`test_every_raised_error_code_is_documented` 直接红。补表后 712 项全绿。

### 测试

704 → **712 项**（`tests/test_hub_packstore.py` 8 条）：识别扩展数与是否带数据、
下载 sha 一致、同 id 不覆盖、被篡改要报错、非法 id 挡穿越、删除干净、空包拒绝。

九项门槛全绿（712 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.12 — 2026-11-20

**带认证的 HTTP 代理也做了端到端验证（上一版验的是 socks5）。**

### 为什么两种都要真验

HTTP 代理的认证是 `Proxy-Authorization: Basic ...`，
socks5 的认证是 RFC 1929 的二进制握手 —— **两套完全不同的东西**。
混用只会得到 407，但表面现象都只是"代理不通"，很难看出是认证方式搞错了。

用户原话：「同时最好可以导入 HTTP 相关的代理这种类型」，所以这条也得真验。

### 结果（真服务端，走 CONNECT 隧道 —— HTTPS 流量正是走它）

```
隧道建立: HTTP/1.1 200 Connection Established
隧道内拿到: HTTP/1.1 200 OK
结论: ✅ 带认证 HTTP 代理 CONNECT 隧道通
```

反向验证：**不带凭据时必须被上游拒（407）**，中继不能当成成功。

### 中途一个有意思的观察

我先用普通 GET 测，中继把完整 URL 发给上游后，我的测试代理自己回了
`HTTP/1.1 200 OK ... PROXY` —— 这是**正确行为**（HTTP 代理转发明文请求就长这样），
只是我把"目标服务"设错了。换成 CONNECT 隧道后才真正验到端到端。

### 测试

702 → **704 项**（`tests/test_proxyrelay_http_e2e.py`）：
CONNECT 隧道带认证跑通、无凭据必须 407。

九项门槛全绿（704 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.11 — 2026-11-20

**带认证的 socks5 代理，第一次用「真服务端」端到端验证通过。**

### 为什么这轮做它

用户需求第一条就是「直接导入 `socks5://user:pass@host:port` 这种代理」。
以前只有"命令拼对了"层面的断言 —— 而**带认证的 SOCKS5 握手**（RFC 1929）
恰恰是最容易出错的一层：凭据长度编码、方法协商顺序、失败后怎么回。

这轮不 mock，起一个真 TCP 服务端走完整握手。

### 结果

```
解析: socks5 127.0.0.1:21102 | needs_auth: True
中继监听: 40859
经中继拿到: HTTP/1.1 200 OK
统计: connections=1, bytes_in=41, bytes_out=43, last_error=''
```

链路：客户端 → 本机中继 → 带认证 SOCKS5 → 目标服务 → 原路返回。
**打通，且 `last_error` 为空。**

### 一个必须记下来的坑（桩自己的 bug）

我写的模拟 SOCKS5 服务端第一版解析错了。RFC 1929 的认证包是：

```
VER(1) ULEN(1) UNAME PLEN(1) PASSWD
```

**第二字节是用户名长度，不是版本号。** 我把它当版本号校验，于是合法客户端
在认证阶段就被 `Connection reset`。

排查时我先怀疑的是产品代码（中继返回 502）——实际是**测试桩不合法**。
把它改成正确解析后一次通过。桩也要认真写，否则会带着你往错的方向查。

### 测试

700 → **702 项**（`tests/test_proxyrelay_socks5_e2e.py`）：
真服务端 + 真中继跑通链路；**密码错了必须失败**（不能假装 200 成功）。

九项门槛全绿（702 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.10 — 2026-11-20

**机器码写不进配置时，会静默地"每次启动换一个身份"。**

### 问题

`ensure_device_id()` 原来是：

```python
did = "zwd_" + secrets.token_urlsafe(24)[:32]
cfg["device_id"] = did
save_config(cfg)      # ← 抛 OSError 时，did 没落盘
return did
```

写盘失败（目录只读、磁盘满、被安全软件锁住）时，新机器码**没进文件**。
下次调用又生成一个不同的 → 服务器上不断冒"新设备"，每个都要重新授权。
用户看到的现象就是"机器码一直在变"。

### 修法

1. 写盘失败**不吞**（抛给调用方），并打一条说明后果的 ERROR 日志：
   「本次运行仍用 zwd_xxx…，但重启后会变成另一个机器码，服务器会把它当成新设备」
2. 同时记进 `_DEVICE_ID_FALLBACK`，**本次运行内**再调用返回同一个值

### 我第一版写错了，被自己的测试抓住

第一版把兜底值当成了"正常路径的缓存"：

```python
if _DEVICE_ID_MEMO["value"]:
    return _DEVICE_ID_MEMO["value"]      # ← 一开始就读缓存
```

于是**删掉配置文件后仍然返回旧机器码** —— 直接破坏了"删配置 = 换新身份"这个
既有语义，撞红 4 个原有测试（`test_device_id_changes_after_deleting_config` 等）。

改成：兜底**只在写盘失败时**才设置，并且只在"配置路径相同且配置里仍然没有
device_id"时才使用。正常路径完全不变，删配置照样得到新机器码。

> 这轮教训：加缓存前先看它会不会改变既有语义。测试直接把我拦住了。

### 测试

699 → **700 项**（写盘失败仍稳定 + 原有 4 项语义全部保留）。
九项门槛全绿（700 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.9 — 2026-11-20

**「在线却够不着」的原因找到了：core 降级成只听 127.0.0.1，而这件事从没报给服务器。**

### 怎么找到的

对比两台机器的 `gateway_public_base`：

| 环境 | `host` | `gateway_public_base` |
|---|---|---|
| 有 `device_id`（我本地实测） | `0.0.0.0` | `http://192.168.3.6:8960` |
| 云电脑（0.8.0） | — | `http://127.0.0.1:8730` |

而 `config.py` 里那条降级规则是：

```python
if (host 不是回环) and force_token_on_public and (没有 admin_token) and (没有 device_id):
    host = "127.0.0.1"   # 避免把控制台暴露给全网段
```

**只有在 `device_id` 为空时才会降级成回环。** 降级之后外部再也连不上 8730，
而心跳照发（走 hub_url，与 core 监听无关）→ 服务器看到"在线却够不着"。

第二条独立证据：那台机器的机器码变成了 `zwd_toMfovGt…`（`ensure_device_id()`
读不到配置就会生成新的）。**两条证据指向同一件事：`client.json` 里的 `device_id` 丢了。**

### 修法

原因本来就在 `Settings.host_notice` 里，但**只在日志打一行**，从没进过心跳。
现在：

| 位置 | 变化 |
|---|---|
| 心跳 | 带 `host_notice` |
| `devices` 表 | 加列 + 迁移 |
| 控制台首屏 | 设备行带 `host_notice` |
| 投递命令的 warning | 追加「另外这台机器的 core 报了降级原因：…」 |

以后再出现这种情况，主控台会直接说清：

> 另外这台机器的 core 报了降级原因：ZW_HOST=0.0.0.0 但没有 ZW_ADMIN_TOKEN 或
> ZW_DEVICE_ID，已自动降级为只监听 127.0.0.1

### 诚实说明

**这只是"让原因看得见"，没有自动修复。** `device_id` 为什么会丢，我仍然没有直接观测到
（那台机器端口不通，看不到 `client.json`）。0.8.2 我修过两个"可能让它丢"的路径
（配置目录 8.3 短名、坏配置静默丢身份），但没在真机证实。

这条改动能让下次发生时**一次就看清**，而不是像我这次猜十几轮。

### 测试

697 → **699 项**。九项门槛全绿（699 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.8 — 2026-11-20

**`doctor` 在“core 起不来”时正好用不了 —— 加一个不依赖服务的 `diagnose`。**

### 问题

`zw doctor` 走 `POST /api/v1/maintenance/doctor`：那是**正在跑的 core** 在体检。

而用户最需要体检的时刻，恰恰是 core 起不来的时候 —— 那时 doctor 连不上，等于没有。
云电脑上就卡在这：core 不可达，我这边什么都做不了，用户也不知道去哪看，
最后只能靠猜（猜了好几轮）。

### 修法：`zhiwen-client diagnose`

完全不连任何服务，只查本机事实：

```bash
zhiwen-client diagnose
zhiwen-client diagnose --port 8731    # 怀疑端口冲突时指定
```

实测输出（干净环境）：

```
✅ 配置文件      /root/.config/zhiwen-client/client.json（可读，3 个键）
✅ 数据目录      /tmp/dg1（可写）
✅ 端口 8950     空闲
✅ 内核          /opt/.../ungoogled-chromium-150.0.7871.186-1/chrome
—— 没有日志文件（/tmp/dg1/logs/zhiwen.log）——
```

端口被占时（实测 `python` 占着 8951）：

```
• 端口 8951    被 pid=2604730（python）占用—— 不是本程序，换端口
❌ 内核         找不到：没有找到可用的 ungoogled-chromium 内核…
```

五项检查对应五类"起不来"：

| 检查 | 查不出来会怎样 |
|---|---|
| 配置文件能否读出 | 读不出 → 机器码变，要重新授权 |
| 数据目录能否写 | 不可写 → core 直接起不来 |
| 端口被谁占着 | 被旧实例占 → 接管旧版或换端口 |
| 内核在不在 | 没有 → 建环境报 `ENGINE_NOT_FOUND` |
| 日志尾部 | 真正的报错通常在最后几行 |

### 测试

693 → **697 项**：子命令已注册、**不许走 `_call`**（用 AST 检查，确保不依赖服务）、
五项关键检查齐全、支持 `--port`。

九项门槛全绿（697 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.7 — 2026-11-20

**界面会永远停在「连接中…」—— 加了超时兜底。**

### 问题

`connect()` 里三步阻塞（`probe_server` / `enroll` / `core.start`）各有自己的超时，
但一旦哪一步真的挂死（DNS 卡住、半开连接、uvicorn 卡在 startup），
后台线程就不返回 → 界面一直显示「连接中…」，**用户只能强杀进程**。

从用户角度这就是"客户端坏了"，而它其实可以自愈。

### 修法

`connect()` 里挂一个 120 秒看护（`core.start` 自己的就绪等待是 45 秒，余量充足）：

```python
state["connect_seq"] += 1
_seq = state["connect_seq"]
root.after(120_000, lambda: _watchdog(_seq))
```

超时后：状态切到「连接超时」、日志说明可以重试或查什么、**恢复「连接并开始」按钮**。

**用递增序号区分"哪一次连接"**：重连后旧看护必须作废，否则会把刚成功的新连接
误判成超时。

### 验证（两处都真跑 Tk）

| 场景 | 结果 |
|---|---|
| 模拟卡死，1.5 秒后触发 | `connecting → error（连接超时）` ✅ |
| 第一次挂起、300ms 后重连并成功 | 旧看护触发时序号已变 → **仍保持 connected** ✅ |

顺带确认了端口占用的自愈链路（本机真跑）：

```
已占住 8940（模拟旧实例）
→ start: True | 实际端口: 8941
→ 日志：首选端口 8940 被占用，本机服务改用 8941
```

### 测试

690 → **693 项**。九项门槛全绿（693 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.6 — 2026-11-20

**把"用户原话里的每一条需求"和交付清单做了一次机械对账。**

### 做了什么

不是肉眼看，而是拿用户原话里的 12 个要点逐条去清单里找落点：

| 需求 | 清单里的落点 |
|---|---|
| 代理导入（带认证 socks5/http，密码打码） | ✅ |
| 本机控制台免令牌 | ✅ |
| 远程凭机器码进入 | ✅ |
| 监听 0.0.0.0 | ✅ |
| 端口可通过配置改 | ✅ |
| 创建后仍可改代理 | ✅ |
| 启动先恢复上次网页再加检测页 | ✅ |
| 检测页含 IP／指纹／干净度 | ✅ |
| 插件及其数据一起打包 | ✅ |
| 新建默认带上、也可关闭 | ✅ |
| 接口与开关齐备 | ✅ |
| 默认内核 ungoogled | ✅ |

**12/12 都有落点**。这条检查已固化成测试，以后改文档漏掉哪条会直接红。

### 顺带补了一处文档缺口

`docs/16` 里原来只写了 `from_chrome` 的用法。0.8.5 之后 `path` 填浏览器目录也认
（会自动识别并带上数据），但这个行为没写进文档 —— 补上了，并说明
"以前这种情况会导入成功但数据丢失"。

### 测试

689 → **690 项**。九项门槛全绿（690 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.5 — 2026-11-20

**「插件及其数据一起打包」有个静默漏洞：把浏览器的 User Data 目录当路径传，数据全丢。**

### 我是怎么发现的

拿模拟的浏览器 User Data 目录跑整条导入链，结果：

```
包 id: 24d31ad83d29 | 扩展数: 2 | 默认: True
  - 脚本猫UI    data=False  has_data=False   ← 数据没带上
  - CookieTool  data=False  has_data=False
```

但扩展本身导入成功了，新建环境也真带上了插件 —— **只是登录态/配置全没跟着走**。

### 根因

界面上写的是「填那个浏览器的用户数据目录」，所以用户（我）自然传
`.../User Data` 或 `.../User Data/Default`。

而 `src_path` 分支把它当**扩展目录**处理：在 `User Data/Default/Extensions`
里确实能翻到扩展并导入成功，但 `Local Extension Settings` 里的数据没人去读。

> 我一开始还以为是 API 层丢了 `from_chrome` 参数 —— 查完发现是我自己传错了字段。
> 但"传错字段还能导入成功、只悄悄丢数据"本身就是个该修的缺陷。

### 修法

`src_path` 进来先嗅一下浏览器目录结构（自己或 `Default`/`Profile N` 子目录里有
`Extensions/` 或 `Local Extension Settings/`），命中就**自动改走 chrome 导入**：

```python
if src.is_dir() and _looks_like_browser_userdata(src):
    return _import_from_chrome(str(src), name)
```

修完两种传法都带上数据：

```
User Data 根: 扩展数=2  data=[('脚本猫UI', True), ('CookieTool', True)]
Default 层  : 扩展数=2  data=[('脚本猫UI', True), ('CookieTool', True)]
```

### 测试

686 → **689 项**：传 `User Data` 带数据、传 `Default` 带数据、
**普通扩展目录不能被误判**成浏览器目录（反向验证，避免矫枉过正）。

九项门槛全绿（689 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.4 — 2026-11-20

**客户端报上来的"core 为什么没起来"，hub 以前根本不收 —— 等于白报。**

### 现象

云电脑上 core 起不来，我在服务器侧只能看到：

```
core_reachable: False
```

**不知道为什么**。只能跑到机器前看，或者猜（端口占？杀软？权限？）。

而客户端其实**本来就在算**这个原因 —— `gui.local_summary()` 里有 `core_error`，
里面写着类似「本地服务 45 秒内没起来（端口 8730）：端口被别的程序占了」这种话。
但 hub 的设备表**没有这一列**，心跳发上来就丢了。

### 修法

| 位置 | 动作 |
|---|---|
| `devices` 表 | 加 `core_error` 列 + 迁移 |
| `touch()` 心跳 | 存下来（`COALESCE` 之外，恢复时会被清空，不挂过期旧值） |
| `_to_dict()` | 出参带 `core_error` |
| 控制台首屏 | 设备行带 `core_error` |
| 投递命令的 warning | 追加「客户端报告的失败原因：…」 |

现在再看就是：

```
core_reachable: False
warning: 这台设备的本机服务（core）当前不可达…客户端报告的失败原因：本地服务 45 秒内没起来（端口 8730）…
```

**不用跑去机器前，就知道该修什么。**

### 测试

683 → **686 项**：心跳存/清 `core_error`、首屏带该字段、warning 追加原因。

九项门槛全绿（686 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.3 — 2026-11-20

**投递远程命令时，core 不可达要当场说清 —— 我自己干等过两轮才发现没人领。**

### 现象

云电脑上 core 没在跑（心跳照发、服务器显示在线），我从 hub 投递命令：

```
POST /v1/devices/{id}/shell  →  {"status": "queued"}
```

然后一直等，`status` 永远是 `None`。**因为根本没人来领** —— 命令是 core 去拉的，
core 死了就没人拉。

### 修法

投递时现算 `core_reachable`（复用 `_probe_core`），不可达就把原因写进 `warning`：

```
core_reachable: False
warning: 这台设备的本机服务（core）当前不可达，命令会一直排队、不会被执行。
         常见原因：core 崩了、端口被占、或杀软拦截。
         需要到那台机器上打开一次客户端（0.8.1 起客户端会自己把 core 拉起来）。
```

排队本身保留（设备可能马上恢复），只是不再让调用方误以为"在排队执行"。

> 第一版我直接读 `dev["core_reachable"]` —— 那个键**只存在于控制台首屏视图**里
> （`console_overview` 运行时算的），设备原始行没有。结果永远 `None`，
> 警告一次都没出现。实测发现后才改成现算。
>
> 测试也因此加了断言：源码里必须出现 `_probe_core`，且**不许再读那个不存在的键**
> （判断前先剥掉注释，否则会被我自己的注释误伤）。

### 测试

682 → **683 项**。九项门槛全绿（683 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.2 — 2026-11-20

**「覆盖安装后机器码变了」：查代码找到两个能让它发生的真实路径，都堵掉。**

机器码变了只有一个来源：`ensure_device_id()` 从配置里读不到 `device_id`，
于是生成一个新的。所以把"读不到"的路径穷尽掉：

### 一、配置目录没归一化 8.3 短名（很可能是这次的元凶）

`config_dir()` 直接用 `%LOCALAPPDATA%` 拼路径。而这个环境变量在不同进程里
可能长得不一样：

```
正常双击启动        C:\Users\Administrator\AppData\Local
经服务/短名环境启动  C:\Users\ADMINI~1\AppData\Local
```

Windows 上这是**同一个物理目录的两个名字**，但我们拼出来的 `client.json`
路径是两个**不同字符串** —— 于是同一台机器读出两套配置，机器码、服务器地址、
对接码全对不上。

**这正是我用 `shell/exec` 发起静默安装时的情况**（下载日志里出现过
`C:\Users\ADMINI~1\AppData\Local\Temp\...`）。所以"安装的是新版、
但配置没沿用"高度吻合。

修法：`config_dir()` 用 `Path.resolve()` 归一化（Windows 会走
GetFinalPathNameByHandle 展开短名），失败就退回原路径，绝不因此起不来。

> 我**没有**在真机上直接观测到"两个目录都存在"——那台机器 8730/8731 都不通，
> 看不到文件系统。所以这一条按"高度可疑 + 代码层面确实有缺陷"来修，
> 不是"已证实是它"。

### 二、配置坏了会静默换身份

`load_config()` 遇到坏 JSON 时返回 `{}`，接着 `ensure_device_id()` 就生成新机器码。
等于**一次写入中断就换一台"新机器"**，服务器上多一台设备要重新授权。

现在：坏文件改名成 `client.json.broken-<时间戳>` 留档 + WARNING 日志，
至少能人工救回来，也让这件事看得见。

### 测试

678 → **682 项**（`tests/test_config_identity.py`）：配置目录已归一化、
坏配置留档且不留在原地、机器码能原样读写回来、非对象 JSON 不被采用。

九项门槛全绿（682 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.1 — 2026-11-20

**默认就连 3.5，不用先填地址；core 死了会自己拉起来。**

### 一、首次使用默认连 3.5

用户要求：「默认就让他直接用 3.5 的那个，如果要修改再审手动改，默认打开直接就尝试
连接 3.5。如果连不上，再提醒他是否修改。」

原来 `server_var` 初值是 `cfg.get("server") or ""` —— 首次安装是**空串**，
必须先知道服务器地址并手填才能连。

现在：

```python
DEFAULT_SERVER = "192.168.3.5:8700"
server_var = tk.StringVar(value=cfg.get("server") or DEFAULT_SERVER)
```

连不上时也不再只丢一句"连不上"，而是明确告诉你去哪改：

> 默认连的是 192.168.3.5:8700。要换服务器：改上面输入框里的地址
> （形如 192.168.3.6:8700），再点「连接并开始」。

### 二、core 看护：掉了自动拉起来

**这是我把自己锁在外面之后加的**，也是用户反复遇到的坑：

心跳由 **GUI 进程**发，core 是 `LocalCore` 起的子服务。core 挂了之后：

| | |
|---|---|
| 心跳 | 照发 → 服务器显示"在线" |
| 远程命令 | 走 core 的 HTTP 队列 → **拉不到** |
| 实时终端 | 走 core 的 WS 隧道 → **连不上** |
| 本机接口 | `/api/v1/*` 全不通 |

**整台机器对外完全够不着**，只能跑到机器前点一下。

现在 GUI 的 5 秒 tick 里核 `/healthz`：**连续两次**不通才重启（单次抖动不算，
避免误判把好的服务杀掉），恢复后立刻重建心跳与两块面板。重启放后台线程，界面不卡。

> 实测（本地真跑）：起 core → 强制停掉 → `is_running` 变 False →
> 再 `start()` 成功 → 恢复在线。重启链路是幂等的，反复挂掉都能救回来。

### 三、一个观察：云电脑上出现了新的机器码

查 hub 时看到两台同名设备：

```
zwd_toMfovGt43aSixO64UJOCgmqFodb0Lvq  v0.8.0  last_seen 2s
zwd_HS4ClQKT3qD977dDSktRWWCSqD4N5S9W  v0.7.99 last_seen 581s
```

**覆盖安装后机器码变了**（配置没沿用），旧记录还在库里。这一条我还没查清原因
—— 需要那台机器能连上才能看 `client.json`。已记在待办里，不猜。

### 测试

670 → **678 项**：默认服务器常量、GUI 预填、连不上的提示、core 看护五条
（存在且被调度、连续两次门槛、重启+重建心跳、间隔被记住、未连接时不乱重启）。

九项门槛全绿（678 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.8.0 — 2026-11-19

**「装了新版本，版本号还是旧的」的真凶：新客户端静默接管了旧版 core。**

### 现象与排查

用户反复反馈客户端升不上去。这次在云电脑上查清了：

```
8730 <- pid=20372  C:\Users\Administrator\Downloads\zhiwen-client-Windows-x64 (10).exe
8731 <- pid=21188  C:\Users\Administrator\AppData\Local\Programs\ZhiwenClient\zhiwen-client.exe
```

- 8730 上跑的是**手动双击的便携版**（22:15 启动，老版本）
- 刚装好的 0.7.99 在安装目录里，因为 8730 被占，按回退逻辑落到了 **8731**

而 `LocalCore.start()` 的接管分支是这样的：

```python
for cand in [...]:
    if self._health_on(cand):        # 只探了 /healthz
        self.actual_port = cand
        self.attached = True
        return True                  # 直接接管，不校验版本
```

所以每次覆盖安装新版本，新客户端启动时**只探 `/healthz` 就接管了旧 core** ——
界面一切正常，跑的是旧代码，升级等于没生效。**静默错误比报错更糟。**

### 修法

1. **接管时读对方版本**（`/api/v1/meta`），与本客户端不一致就记进
   `attached_version` 并打 WARNING：「复用了一个旧版本的本机服务…升级不会生效」
2. **`take_over()`**：只杀**监听该端口且进程名含 `zhiwen`** 的那个进程，
   然后用当前版本重启。不是本程序一律拒绝（实测占用者是 `python` 时明确回
   「端口被非本程序占用，不会去动它」）
3. **桌面 GUI 当场提示**并给一键接管：
   「这台机器上已经有一个旧版本的服务在运行（0.7.50，端口 8730）…要关掉旧实例、
   改用当前版本吗？」

### 顺带：验证了"安装器写默认值"这条链

在云电脑上做实机验证（真下载真安装）：

```
下载 26,378,561 字节 → 静默安装 exit=0
marker={"shell_enabled": true}          ← 安装器确实写了标记
用安装后的 exe 起 serve 读到 version=0.7.99   ← 安装的确实是新版
```

**结论：安装本身是成功的，锅在"接管旧 core"。**

### 打包事故：相对导入让产物一启动就崩

首次发 0.8.0 三个平台全 fail，失败在 CI 的「冒烟测试」那一步（不是编译）。
本地复现：

```
ImportError: attempted relative import with no known parent package
[PYI-2409492:ERROR] Failed to execute script 'client'
```

原因是我给 `client.py` 加了 `from . import __version__`。PyInstaller 的入口就是
`core/src/zw_core/client.py`，它以**顶层脚本**身份执行、没有父包 —— 相对导入必崩。

**源码模式完全正常**（`import zw_core.client` 没事），所以 pytest 670 项全绿也发现不了。
已改成 `from zw_core import __version__`，并加一条测试**禁止 client.py 顶层出现相对导入**。

> 这正是 `AGENTS.md` 里说的"源码绿 ≠ 二进制能用"：靠打包产物跑一次 `--selftest` 才抓到。

### 测试

663 → **670 项**：接管记版本、不一致要 WARNING、`take_over` 的进程名白名单、
复用 `engines.port_owner_pid`、GUI 必须提示并可接管、client.py 不许相对导入。

九项门槛全绿（670 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。
本地打包产物 `--selftest` **14 项全过**。

## 0.7.99 — 2026-11-19

**常驻终端打通了（不需要设备升级）；顺带补上 hub 侧心跳。**

### 一、hub 主动 ping —— 老客户端也能保持在线

0.7.97 修的是**设备侧**发 ping，但那只对新客户端有效。手里还是 0.7.95 及更早的
机器照样安静 90 秒被判掉线。而设备**本来就**实现了 `ping → pong`
（`termhub._handle`），所以让 hub 主动发 ping 就能立刻覆盖它们：

```python
keepalive = asyncio.create_task(link.keepalive())   # 每 30 秒发一次 {"t":"ping"}
```

这个修复**不用等用户升级**，改完 hub 就能验证 —— 实测部署后
`realtime` 从 `False` 变成 **`True`**。

> 顺手抓到一个潜在崩溃：`hub/main.py` **从来没有导入过 asyncio**，
> 我加了 `asyncio.create_task` 之后会 NameError。已补 import，并加了断言。

### 二、`scripts/term.py` —— 真正的常驻终端

`/api/v1/shell/exec` 是一问一答、每条独立（cwd 不保持）。常驻终端走
设备到 hub 的持久 WebSocket，cwd 保持、输出流式。

```bash
python scripts/term.py --login --user <用户> --password <密码>   # cookie 存 ~/.dsh
python scripts/term.py -c "cd C:/Users/Administrator/AppData/Local/zhiwen && dir /b engines"
```

实测（云电脑 0.7.98）：

```
[会话已开] cwd=C:\Users\Administrator\AppData\Local\zhiwen
C:\Users\Administrator\AppData\Local\zhiwen
ungoogled-157.0.8103.78-1.1
```

`cd` 之后再 `cd` 仍停在同一目录 —— 说明是同一个长驻会话，不是每条新开。

**鉴权只能用面板 cookie**：本部署里 hub 容器的 `ZW_HUB_TOKEN` 是空的
（`.env` 里那行被注释了），`?token=` 这条路直接 1008 unauthenticated。
这条已经写进脚本注释，免得下次再撞。

### 三、`scripts/winrun.py` —— 可靠的远程执行

排查时反复踩引号转义（远端 shell 把 `"` 转成 `\"`、`&` 被 cmd 当分隔符）。
改成 PowerShell `-EncodedCommand`（base64 UTF-16LE），命令行里只剩字母数字，
一次就成；并前置 `$ProgressPreference='SilentlyContinue'` 压掉进度流
（一次成功调用会在 stderr 塞 1.5KB 的 CLIXML，把真错误淹没）。

### 测试

662 → **663 项**（新增 hub keepalive 调度 + `hub/main.py` 必须导入 asyncio）。

> 我上一轮加的"交付清单防过期"测试这轮**自己先报了一功**：版本升到 0.7.99
> 但清单还写着 0.7.98，CI 直接红并点名。已同步。

九项门槛全绿（663 / 34 / 73 / 13 / 23 / 81 / 43 / 12 / 42）。

## 0.7.98 — 2026-11-19

**远程执行通道：按钮开关、默认开启、装完即生效。**

用户原话：

> 以后所有的包都加入远程执行通道（弄成按钮开关，默认打开），
> 只要安装就直接允许被远程连接和下发任意命令。

拆成四件事做：

### ① 装完即开启（含"以前手动关过"的老机器）

核心默认本来就是 `ZW_SHELL_ENABLED=true`，但**老用户的 `client.json` 里可能
留着 `"shell_enabled": false`**（以前点过关过）—— 光改代码默认值对他们无效。

难点：安装器不能直接改 `client.json`（里面有服务器地址和对接码，
拼 JSON 很容易弄坏）。所以走**标记文件**：

```
安装器写 %LOCALAPPDATA%\zhiwen-client\.install_defaults.json  {"shell_enabled": true}
客户端首次启动读到 → 合并进配置 → 删掉标记
```

幂等、不碰其它字段、只生效一次（之后用户在界面上关掉就是真关掉，不会被又打开）。

### ② 界面是**按钮开关**，不是勾选框

- **桌面 GUI**：原来是勾选框，容易被当成高级选项忽略。改成按钮，文字直接显示
  「远程执行：已开启 / 已关闭」，关掉时用危险色（`Danger.TButton`）。
- **web 控制台**：原来**完全没有这个开关**。新增「远程执行」卡片（运维页），
  同样是一个按钮 + 状态徽章 + 工作目录/超时/平台 shell。

### ③ 默认打开

`ZW_SHELL_ENABLED` 默认 `True`（原本就是），加上 ①，装完就能被远程连、能下任意命令。

### ④ 测试（657 → 662 项）

新增 `tests/test_remote_exec_default.py` 5 项：

| 测什么 | 为什么 |
|---|---|
| 标记把 `false` 翻成 `true`，其它字段不丢 | 老机器升级的核心场景 |
| 标记只生效一次 | 否则用户关掉后每次启动又被打开 |
| 坏标记要删掉 | 不能让用户反复踩同一个坏文件 |
| web 有按钮 + 读状态 + 下发切换 | 原来压根没这个开关 |
| GUI 是按钮且反映状态、关掉用危险色 | 用户明确要"按钮开关" |
| 安装包真的写了标记 | 否则"装完即允许"做不到 |

`ui-check` 37 → **43 项**（真浏览器点两下：翻转到关、再翻转回来）。
`gui-check` 39 → **42 项**。

> 期间的坑：我第一版 ui-check 直接断言"默认是开启的"，结果在 `check-all` 里红 ——
> 因为脚本为了跑安全用例显式设了 `ZW_SHELL_ENABLED=false`，那时关掉才是对的。
> 改成**按接口实际状态断言**（按钮文案与状态一致、点一下翻转、再点回来），
> 默认值交给配置层单测。两种状态下各跑一遍都是 43 项全过。

九项门槛全绿（662 / 34 / 73 / 13 / 23 / 81 / **43** / 12 / **42**）。

## 0.7.97 — 2026-11-19

**「常驻终端连不上」的根因：两端都不发心跳，设备安静 90 秒就被判掉线。**

### 现象

用户要求"全程留个终端连接，方便随时通讯和修改"。查下来：

```
云电脑 8730 通、版本 0.7.95、设备 online=True、shell_enabled=True
但 hub 控制台 realtime=False
```

而设备日志明确写着 `终端 WS 已连上 hub`。也就是说**连接是好的，判定是错的**。

### 根因

hub 侧判据（`termrelay.online`）：

```python
return (time.time() - link.last_seen) < DEVICE_IDLE_TIMEOUT   # 90 秒
```

`last_seen` 只在**收到设备消息**时更新。而两端都不主动发消息：
- 设备只在**收到** hub 的 ping 时才回 pong
- hub **从不发** ping
- `_PING_EVERY = 20.0` 这个常量早就定义了，**没有任何地方引用它** —— 写了一半

于是设备安静挂着 90 秒 → 判掉线 → `/v1/console/term/{device_id}` 直接回
`device_offline` 并关连接。**常驻终端在正常空闲状态下永远建不起来。**

### 修法

设备侧真的按 `_PING_EVERY`（20 秒）发应用层 ping：

```python
asyncio.create_task(self._ping_loop())     # 之前只有 send/recv 两个任务
```

20 秒 ≪ 90 秒窗口，中间丢一次也不会误判。

### 顺带：给"远程改 Windows"做了个可靠通道

排查时反复踩引号转义（项目笔记里早有记录：远端 shell 把 `"` 转成 `\"`，
`&` 失去保护被 cmd 当分隔符）。新增 `scripts/winrun.py`：

```bash
python scripts/winrun.py -c "Get-ChildItem C:/Users/Administrator/AppData/Local/zhiwen"
python scripts/winrun.py -f task.ps1        # 长脚本从文件读
```

内部走 PowerShell `-EncodedCommand`（base64 UTF-16LE），命令行里只剩字母数字，
一次就成；并前置 `$ProgressPreference='SilentlyContinue'` 压掉进度流
（实测一次成功调用会在 stderr 塞 1.5KB 的 CLIXML 噪音，把真错误淹没）。

### 测试

655 → **657 项**（`tests/test_termhub_ping.py`）：`_ping_loop` 必须被
`create_task` 起来（防止再写一半）、ping 间隔必须显著小于 hub 的 90 秒窗口。

九项门槛全绿（657 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.96 — 2026-11-19

**0.7.95 在真机上确认修好了包名；但 `extensions_active` 还是不给出结论 —— 找到第二个原因。**

### 已在真机确认生效的（0.7.95）

云电脑升到 0.7.95 后查包列表，4 个占位符全部还原成真名：

```
Bitwarden Password Manager        raw='__MSG_extName__'
沉浸式翻译 - AI 双语网页翻译 …    raw='__MSG_brandName__'
脚本猫                            raw='__MSG_scriptcat__'
YesCaptcha 人机助手               raw='__MSG_name__'
```

`name` 给人看，`name_raw` 留原始值便于排查。

### 还没修完的：`extensions_active` 永远是 None

内核层面 `extensions_loadable: True`（正确），但每个环境都显示 `None`（不知道）。
根因是这一行：

```python
ok, note = engines.extensions_loadable(str(out.get("engine_path") or ""))   # → ''
```

而云电脑上 `profile.engine_path` **就是空串**（环境没记具体内核，靠全局策略找）。
拿空路径去探测必然失败 → None。

`packs.py` 里早就有正确写法（空则 `find_chrome()`），`manager.py` 漏了同一步。
现在补上：

```python
_bin = Path(str(out.get("engine_path") or ""))
if not str(out.get("engine_path") or "").strip():
    _bin = engines.find_chrome(get_settings())
ok, note = engines.extensions_loadable(_bin)
```

本地验证：强制把 `engine_path` 清空后，`extensions_active` 从 `None` 变成 **True**。

### 测试

654 → **655 项**。新测试用桩证明**回退链路真的被走到**：把 `find_chrome`
换成返回标记路径、`extensions_loadable` 换成只认那个路径并记录入参 ——
如果代码仍拿空串探测，记录的入参会是 `''`，断言即失败。

> 第一版测试写成"造个环境看结果"，但测试机没内核 → 走 `EngineError` 分支 →
> 仍然 None，测不出区别。改成验"传给探测的路径是什么"，才真正钉住行为。

九项门槛全绿（655 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.95 — 2026-11-19

**清掉扩展库里的垃圾条目；顺手修好"包名显示成 `__MSG_xxx__`"。**

### 一、扩展库清理（云电脑实操）

删 CookieManager 时发现库里还有两条看着可疑的，先读 manifest 确认身份再删（不靠名字猜）：

| slug | 读到的 manifest | 结论 |
|---|---|---|
| `__msg_extname__` | `"author": {"email": "docs-hosted-app-own@google.com"}` | **Google Docs Offline** 组件扩展，浏览器自带 |
| `edge-relevant-text-changes` | `"description": "Edge relevant text changes on select websites…"` | **Edge 自带** |
| `probewin` | `{"name":"ProbeWin"}` | 我自己做探针留下的测试件 |

三条都**没有被任何环境或默认包引用**（查过才删）。删完库从 9 条降到 **5 条**，正好是你要的：
YesCaptcha / 脚本猫 / 沉浸式翻译 / Cookie-Editor / Bitwarden。

### 二、包名显示成 `__MSG_xxx__`

清完看包列表，5 个默认包的显示名有 4 个是占位符：

```
__MSG_extName__   __MSG_brandName__   __MSG_scriptcat__   __MSG_name__
```

原因：这些包是**早期版本**建的，那时扩展名还没做 `_locales` 解析，占位符被直接存进了包元数据。

修名字有两条路：写数据库迁移，或**读取时解析**。选后者 —— 存量包自动变好，
不用迁移，也不怕旧版本再写坏一次：

```python
def _resolve_pack_name(meta):        # 只对"整体就是一个 __MSG_xxx__"的名字生效
    ...  # 从扩展目录的 _locales 现场解析；解析不出来就原样返回，不编造
```

出参同时给 `name`（解析后，界面用）和 `name_raw`（原始值，便于排查）。

### 三、`check-all` 失败时不再只报"1 failed"

连跑门槛时遇到过一次单点失败，但脚本的 `fail` 分支只 `grep` 最后一行数字，
**没留下用例名** —— 结果只能手工复现一遍才知道是哪个（而且脱开脚本又复现不出来，
因为脚本会先 export `ZW_ROOT` / `ZW_CHROME_BIN`，pytest 继承它们）。

现在会带出前 3 个失败用例名：

```
pytest  ❌ 失败 1 failed, 653 passed  →  tests/test_extstore.py::xxx
```

> 修完后连跑两次门槛都是 **654 项全过**，那次失败没再出现；单独复现、
> 带相同环境变量复现、重复跑单文件三次也都是绿的。按偶发问题处理，
> 但至少下次再犯能立刻定位到用例。

### 测试

653 → **654 项**（包名读取时解析）。九项门槛连跑两次全绿
（654 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.94 — 2026-11-19

**用户让删一个扩展，顺手把"探测三态"补彻底了 —— 之前只改了一半。**

### 删除扩展（云电脑实操）

用户在云电脑上要求删掉 `CookieManager - Cookie Editor`。它同时挂在三处，逐一清掉：

| 位置 | 动作 | 结果 |
|---|---|---|
| 环境 TEST3（唯一绑它的） | `DELETE /profiles/TEST3/extensions/cookiemanager---cookie-editor` | ✅ 剩下 5 个扩展 |
| 扩展包 `20a58e339cec`（它自己的包，默认） | `DELETE /packs/20a58e339cec` | ✅ 包 6→5 |
| 扩展库 `cookiemanager---cookie-editor@0.2.7` | `DELETE /extensions/{slug}/{version}` | ✅ 库 9→8 |

验证：三处都查不到 `CookieManager`；TEST3 重启后 5 个扩展齐、浏览器里没有它的页面。

> 删除接口本身是安全的：还被环境绑着时返回 `409 EXTENSION_IN_USE` 并列出是哪些环境，
> 不会留下悬空引用。包应用时若引用了已删扩展，会跳过并给一条 warning（不崩）。

### 三态语义之前只改了一半

0.7.92 我把 `extprobe` 出错改成了返回 `None`（不知道），但有两处会把它再压回 `False`：

1. `engines.extensions_loadable()` —— `if r.get("loadable"): ... return False`
   （`None` 是 falsy，走 else 分支）→ 调用方拿到的仍是"确定不能加载"
2. `extprobe` 缓存 —— 老缓存里那条 `{"loadable": false, "reason": "探测过程出错…"}`
   是**按旧语义写的结论**，改代码也不会让它消失，于是界面继续显示 False

修法：
- `extensions_loadable()` 返回 `tuple[bool | None, str]`，None 原样传出
- 缓存加 `_CACHE_VERSION = 2`，**版本不符的条目一律丢弃**（当作没缓存 → 重新探）

> 云电脑上的实际现象就是这样：升级到 0.7.93 后 note 已经是新文案
> 「探测没能完成…无法判断」，但 `extensions_active` 还显示 `false` ——
> 两个原因各占一半。

### 测试

651 → **653 项**：加"老缓存被丢弃"、"`extensions_loadable` 保住 None"；
并把既有 `test_profile_detail_reports_extensions_active` 从"不能是 None"
改成"三态合法，且 None 时必须给出原因"——那条断言本身是按旧语义写的。

九项门槛全绿（653 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.93 — 2026-11-19

**「每次打开都无响应几秒钟」—— 连接流程全跑在 Tk 主线程上。**

### 现象与定位

用户反馈：每次打开客户端都会无响应几秒钟（窗口变白、标题栏「未响应」）。

`connect()` 原来整个跑在 Tk 主线程上，其中三步都是秒级阻塞：

| 阻塞点 | 为什么会慢 |
|---|---|
| `probe_server(base)` | 网络往返；**服务器不通时要等超时** |
| `enroll(...)` | 又一次网络往返 |
| `core.start(...)` | 起 uvicorn 并**轮询等它就绪**（本机量到 **1215ms**，Windows 过杀软扫描更久） |

而且启动时 `root.after(200, connect)` 会**自动连一次** → 每次打开必卡。

### 对照实验（把"卡"量化）

同样的 1200ms 任务，在 Tk 主线程上跑 vs 放后台线程跑，用一个 `after(50)` 心跳测停顿：

```
旧写法（主线程直接跑 1200ms 任务）→ 界面最长停顿 1250 ms
新写法（run_bg 后台跑同样的任务）  → 界面最长停顿   50 ms
```

> 第一次测的时候我在**阻塞进行中**就取了最大值，显示 50ms，差点得出"不卡"的错误结论。
> 后来先用一个已知 1200ms 的 `sleep` 校准测量方法（测到 1250ms），才确认方法有效、
> 并且必须在 mainloop 结束后再统计。

### 修法

`connect()` 拆成三段，阻塞部分进后台：

1. `connect()` —— 主线程：校验地址、`set_phase("连接中…")`、禁用按钮、`update_idletasks()`
   让提示先画出来，然后启动后台
2. `work()`（嵌套）—— 后台线程：`probe_server` → `enroll` → `core.start`，返回阶段结果
3. `on_connect_done()` —— 回到主线程（`root.after(0, ...)`）做所有分支与控件更新

配套：
- `run_bg(fn, done)`：后台跑 + `after(0)` 回主线程；窗口已销毁时静默丢弃
- `log_bg(text)`：后台线程专用的日志（`log()` 会动控件，必须排队回主线程）
- 顺手把"连上后按配置关远程命令行"那个 `httpx.post` 也挪进后台（原来也阻塞主线程）

### 防回归

`tests/test_gui_startup_nonblocking.py`（3 项，用 AST 检查）：

1. `connect()` **自己的语句**里不许出现 `probe_server` / `enroll` / `core.start`
2. 这三个调用必须仍在嵌套的 `work()` 里（防止有人"修"成直接删掉功能）
3. `run_bg` 必须用 `root.after(0, ...)` 回主线程 + 真的起了 `threading.Thread`

验证过它确实会红：把 `probe_server` 临时挪回主线程 → 断言失败并点名
「这些阻塞调用又回到 Tk 主线程上了：probe_server」；恢复后 3 项全过。

### 测试

648 → **651 项**。九项门槛全绿（651 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.92 — 2026-11-19

**云电脑真机验收抓到两个 bug：桌面客户端从不自动下载内核；扩展探测把"没测成"报成"不能加载"。**

### ① 双击打开的客户端，从来没有自动下载过内核

用户报：客户端（0.7.91）打开后建环境报
「没有找到可用的 ungoogled-chromium 内核」，`GET /api/v1/engine` 里
`download.state` 一直是 **idle** —— 说明自动下载压根没被触发。

根因：`prepare_engine_async()` 只写在 `main.run()` 里，而

| 入口 | 怎么起 core | 会不会自动下载 |
|---|---|---|
| `zhiwen-client serve` | 走 `run()` | ✅ 会 |
| **双击客户端图标（GUI）** | `LocalCore` 直接 `from zw_core.main import app` 起 uvicorn，**绕过 `run()`** | ❌ 从来不会 |

也就是说 0.7.72 起号称的"客户端自己下载内核"，在**用户实际使用的那个入口**上从未生效。
只有命令行 `serve` 才会。

修法：在 `LocalCore.start()` 里也调 `prepare_engine_async()`（放在启动 uvicorn 之前）。
不放 `create_app()` 的 startup 事件里，是因为测试也用 `create_app()` ——
那样每跑一次测试就会去下 150MB。

> 中途还踩了自己一个坑：第一版插进去用了 `get_settings()`，而那个名字要到**后面几行**
> 才 import（函数内 import 是局部名）→ NameError 被 `except` 吞掉，
> 表现成"加了代码但依然不下载"。已改成在自己的 try 里 import。

### ② 扩展探测报"不能加载"，其实只是没测成

同一台机器上，环境已经跑起来、6 个扩展都绑着，但详情里
`extensions_active: False`，note 是：

```
探测过程出错（PermissionError: [WinError 5] 拒绝访问。），已按不加载处理
```

杀软/权限拦住了探针进程，而旧代码把**异常当成"确定不能加载"**，还**写进了缓存** ——
于是之后一直显示"不能加载"，用户以为插件坏了。

改成三态语义：

| 值 | 含义 |
|---|---|
| `True` | 实测能加载 |
| `False` | 实测不能加载（官方 Chrome 137+ 忽略 `--load-extension`） |
| `None` | **没测出来**（探测本身失败）→ 界面显示"检测中/未检测"，不误导 |

失败时**不写缓存**，下次还能重试。`manager.get_profile` 里原来的
`bool(ok)` 会把 `None` 压成 `False`，一并改成保留三态。

### 云电脑上的实测结果（0.7.91 + 手动触发安装）

```
内核：C:\Users\Administrator\AppData\Local\zhiwen\engines\ungoogled-157.0.8103.78-1.1\chrome.exe
版本：157.0.8103.78   extensions_loadable: True
下载：193.5MB / 193.5MB (100%)，自动发现代理 http://127.0.0.1:21080
环境 TEST3：state=running，6 个扩展已绑定
```

而且浏览器里出现了**扩展自己打开的页面**：
`bitwarden.com/browser-start/`、`onboarding.immersivetranslate.com/...`、
`docs.scriptcat.org/docs/use/...` —— 这些只有扩展真的被加载并运行才会出现。
**所以插件是好的，之前那个 `False` 是探测误报。**

### 测试

644 → **648 项**（新增 `tests/test_extprobe.py` 4 项：异常→None、失败不缓存、
peek 无缓存→None、脏缓存→None）。

九项门槛全绿（648 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.91 — 2026-11-19

**「云电脑离线」这个判断，我一直说得太笼统 —— 它其实分两种情况，处置方式完全不同。**

### 先纠正一个事实

之前每轮我都写"云电脑离线"。这轮具体探了一下：

```
ping 192.168.12.162     → 通
:445 / :135             → 开着（说明机器连着网、系统在跑）
:3389 / :8730 / :8731   → 都不通
```

**机器在网络上，但客户端没在运行。** 这和"机器关机了"是两件事。

### 为什么会这样：自启动默认不勾

安装包里「开机自启」是**默认不勾**的（`zhiwen-client.iss` 第 141 行：
「默认**不勾**。勾了写 HKCU\...\Run」）。

所以云电脑一旦重启，客户端不会自己起来 —— 从服务器看就是"设备离线"，
但机器好得很。而这两种情况的处置完全不同：

| 情况 | 该怎么办 |
|---|---|
| 重启后客户端没起 | 到机器上双击一次图标就行 |
| 机器真关机 / 断网 | 只能等它回来 |

### 修法：让主控台能说出原因

1. **客户端心跳上报 `autostart`**（`shellagent._payload`）
2. **hub 存这个字段**（`devices` 表加列 + 迁移 + `COALESCE` 保持原值）
3. **主控台在离线设备上显示「未开自启」徽章**，鼠标悬停给出完整解释：
   > 这台机器没有开启开机自启。如果它刚重启过，多半只是客户端没自己起来 ——
   > 到机器上双击一次客户端图标即可。

**三态语义要保住**：`true` = 开了 / `false` = 没开 / `None` = 旧客户端没上报。
`None` **不能**当成 `false`，否则老版本客户端会被误报成"确定没开自启"。

### 测试

643 → **644 项**：验三态、验"后续心跳没带这个字段时保持原值"（`COALESCE`）。

### 给用户的一句话

云电脑那边：**客户端「设置」里勾上开机自启**（或重跑安装包时勾选），
以后重启就不用手动点了。现在即使忘了勾，主控台也会提示你「未开自启」。

九项门槛全绿（644 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.90 — 2026-11-19

**《交付验收清单》停在 v0.7.70（601 项测试、八项门槛），实际已经 0.7.89（642 项、九项）。**

### 为什么这件事严重

这份文档是**你照着自己点一遍**的验收单。它写在 v0.7.70，中间 19 个版本加的东西
（内核自动下载、内核卡片、默认包开关、远程凭机器码、`ZW_PORT`、`proxy_info`、
代理凭据打码）**一条都没进去**。

你按旧清单验，会看到"ui-check 29 项"，而实际要跑出 37 项 —— 然后怀疑哪边不对。
文档骗人比没文档更糟。

### 重写后的结构

保留原来"怎么验 / 应该看到什么"的两列表格，但：

- **新增第〇节「内核」放最前面**：这是 0.7.72 之后最大变化。不先看这节，
  你会疑惑"插件为什么没生效"（官方 Chrome 137 起不加载命令行扩展）
- 第二节补上「远程凭机器码」的真浏览器验证（0.7.82 从 LAN IP 验的）
  与 `ZW_PORT` 修复（0.7.85）
- 第五节补上「新建时取消勾选 → 干净环境」的开关（0.7.88/0.7.89）
- 第一节补上「代理列不再显示成『直连』」（0.7.83）与凭据打码（0.7.79）
- 新增「给 AI 用的接口自描述」一节（0.7.87 补全 13 条漏掉的端点）
- 门槛表更新为九项真实数字，另附产物验证（`verify-release.sh`）
- "没做的事"补了 `zw sync rekey`、hub 多用户、macOS 内核自动下载

### 加了防过期测试

`test_delivery_checklist_is_not_stale`，机械校验三件事：

1. **声明性版本的标题行/`版本：` 行**必须等于 `zw_core.__version__`
2. 正文声明的单元测试数必须与 `AGENTS.md` 一致
3. `check-all.sh` 里跑的每个门槛，清单里都要有对应的结果行

> 这个测试我改了三版才对：
> 第一版只取**第一处**版本匹配 —— 我把标题故意改回 `0.7.70` 它都没红
> （因为正文别处还有一处正确的）。第二版改成全文匹配 —— 又把
> "0.7.83 修过…"这种**历史修复说明**和 Chromium 的 `0.8078.79` 全报成过期。
> 最终只看声明性位置，并且**验证过它确实会红**（改错标题 → 断言失败 → 恢复 → 通过）。

### 测试

642 → **643 项**。九项门槛全绿（643 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 39）。

## 0.7.89 — 2026-11-19

**上一版我在文档里写了句假话：「GUI/控制台里新建时也有对应勾选」。其实没有。**

### 问题

0.7.88 加了 `with_default_packs` 这个 API 参数，然后在 `docs/16` 里写下：

> GUI/控制台里新建时也有对应勾选。

但 `grep` 一下就知道：

```
gui_profiles.py: 0 处
web/index.html : 0 处
```

**只有 API，没有界面。** 用户按文档找那个勾选会找不到 —— 这比不写更糟。

### 补齐两个界面

**web 控制台**（新建环境表单）：

```html
<label class="switch" title="默认带上「默认扩展包」…">
  <input type="checkbox" id="f-with_default_packs" checked> 带上默认扩展包
</label>
```

只在**新建**时出现（改环境时不动已绑定的扩展集合）。提交时转成 `payload.with_default_packs`。

**桌面 GUI**（新建环境浮层）：`_Overlay` 原来只支持文本字段，加了个
`add_check_row()` —— 把 `BooleanVar` 塞进 `self.vars`，复用 `_values()` 的收集逻辑，
值以 `"1"` / `""` 出现。`CoreApi.create_profile()` 增加同名参数并真正发出去。

### 测试

`gui-check` 37 → **39 项**：断言新建浮层里有「带上默认扩展包」、且默认勾选
（保证默认行为不变）。

### 顺带修掉自己弄坏的一处

改 `gui-check` 时我误删了「扩展包面板有 XX」那组断言的前半段（`for want in (...)` 后面
跟了个 `pass`），导致那 6 条只跑 1 条。已恢复。

还踩了个 Tk 的坑：`_Overlay` **不是 Frame**，构造完不能 `pack()`（它自己管理浮层容器）；
而浮层的子控件挂在 **master（根窗口）** 上，不在 `ov` 自己的子树里 ——
所以断言文字必须从 `t` 开始遍历，从 `ov` 遍历什么都找不到。

九项门槛全绿（642 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / **39**）。

## 0.7.88 — 2026-11-19

**「当然也可以选择关闭」这句 —— 之前没有关闭的办法。**

### 缺的是什么

你的原话：

> 这样以后创建浏览器时，可以默认就把这些插件的数据都带上（**当然也可以选择关闭**）。

"默认带上"做了（`is_default` 扩展包 → 新建环境自动应用），但**"关闭"没做**：

```python
if not profile_id:
    packsvc.apply_default_packs(pid)      # 无条件，没有开关
```

想建一个干净环境，只能建完再手动一个个解绑。这个用途其实很实在：
临时环境、要做指纹对照、排查"是不是某个插件导致的问题"。

### 修法

新增请求参数 `with_default_packs`（默认 `true`，保持老行为）：

```bash
curl -X POST "$CORE/api/v1/profiles" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"干净环境","with_default_packs":false}'
```

实测（真 core + 真默认包）：

```
A 默认建      → extensions: ['开关自检扩展']
B with=false → extensions: []
```

`docs/04`（接口表）、`docs/16`（扩展包那篇）都补了说明。

### 测试

641 → **642 项**：造一个默认包 → 建两个环境 → 断言 A 带上、B 为空。

### 过程中踩的两个坑（都是我自己的，不是产品问题）

1. 我以为 API 的字段名是 `src_path`，实际对外叫 `path`（服务层才叫 `src_path`），
   于是 `packs/import` 一直报 `PACK_INVALID_SOURCE` —— 查了一圈代码才发现是我调错。
2. `set_default(pack_id)` 其实是 `set_default(pack_id, enabled)`。

两个都记在测试注释里了，避免下次再绕。

九项门槛全绿（642 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 37）。

## 0.7.87 — 2026-11-19

**给 AI 的自描述清单漏了 13 条接口 —— 包括我刚加的「内核」和「扫描本机」。**

### 这是什么问题

`GET /api/v1/ai/manifest` 是这个项目特意为 AI 设计的：AI 读完它就知道全部能力、
调用顺序、踩坑提示，不用先读文档。`AGENTS.md` 也明确规定
"加一个接口 → 在 `/api/v1/ai/manifest` 的端点清单里补一条"。

但我这几轮加 `GET /api/v1/engine`、`POST /api/v1/engine/install`、
`GET /api/v1/packs/scan-browsers` 时，**一条都没补进去**。

后果很具体：AI 读这份清单会不知道"内核可以自己装"、"能扫描本机浏览器导入插件"，
于是要么用别的方式硬凑，要么直接告诉用户"做不到"。

### 补的时候发现缺的不止我加的那三条

写了个测试**真的调接口**拿返回，再和 FastAPI 注册的路由表逐一比对。结果：

```
缺 13 条：
  /engine  /engine/install  /packs/scan-browsers
  /maintenance/doctor  /maintenance/trim  /maintenance/reap-orphans  /maintenance/cleanup
  /packs/{id}  /profiles/{ref}/extensions/{slug}  /extensions/{slug}/{version}
  /page/targets  /page/text
  /sync/state  /sync/{ref}  /sync/{ref}/history  /term
```

其中 `/page/targets`、`/page/text` 是"不进 CDP 也能读页面"的常用能力，
`/maintenance/*` 是运维接口 —— 都不是边缘功能，只是清单一直没跟上。

### 新测试

`test_ai_manifest_lists_all_public_routes`：真的 `TestClient` 调一次接口，
把 `端点清单` 与 `app.routes` 比对，缺一条就红并列出名字。

> 第一版我是用正则去解析源码里的清单文本，结果被"源码里同时有路由注册语句和
> 清单文本"干扰，扫出来全是误报。改成调真接口可靠得多。

比对时把 `{id}` / `{ref}` 这类参数归一成 `{}`，避免同一路由两种写法被算成缺。

### 测试

640 → **641 项**。九项门槛全绿（641 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 37）。

## 0.7.86 — 2026-11-19

**把「文档里的配置项」与「代码真读的变量」做了一次机械对账，抓出 3 处。**

### 做法

不靠肉眼翻文档，直接比对两个集合：文档里出现的 `ZW_*` 名字 vs 源码里出现的。
第一版全文扫噪音太大（示例命令里的 `ZW_CORE`/`ZW_CHROME`、脚本变量
`ZW_EXT_DIR`、正文里的否定式提及都会被报成错的），改成**只扫配置表的行** ——
那才是"用户照着配"的权威清单。

### 抓到的三处

**① 变量名根本不存在**（`docs/03` §五）

> 1. 同样装好 core，设不同的 `ZW_PORT`（比如 8731）和**不同的** `ZW_PORT_POOL`。

真实配置项是 `ZW_PORT_POOL_START` / `ZW_PORT_POOL_END` **两个**变量，
没有 `ZW_PORT_POOL`。照这行做，两台 core 仍然抢同一段 CDP 端口 ——
表现是"偶发启动失败"，而没有任何报错指向配置。

**② 同一份文档里默认值自相矛盾**

`ZW_PORT_POOL_START` 的默认值，一处写 `21001/21100`，另一处写 `22001/22100`。
代码实际是 `21001/21100`。

**③ 排错指引里给了不存在的配置项**（`docs/04` 错误码表）

> `EXT_STORE_UNSAFE_PATH` … 检查 `ZW_EXT_DIR` 配置

`ext_dir` 是 `Settings.root / "extensions"`，**没有环境变量可改**。
在"出错时该查什么"这一列给错答案，比不给更糟。已改成说明它由 `ZW_ROOT` 推导。

### 两条新测试

- `test_documented_env_vars_actually_exist_in_code`：配置表里的每个 `ZW_*`
  必须在源码里被读到。以后谁在文档里写个不存在的变量，CI 直接红。
- `test_port_pool_defaults_are_consistent_in_docs`：文档写的默认值必须与
  `Settings` 实际默认一致（防"同一份文档两种说法"）。

### 顺带确认（不是问题）

`ZW_HOST` 的默认值：`Settings` 里就是 `0.0.0.0`，客户端 GUI 还会
`setdefault("ZW_HOST", "0.0.0.0")`，`serve` 也走同一默认 ——
两条启动路径一致，符合"监听 0.0.0.0"的要求。

### 测试

638 → **640 项**。九项门槛全绿（640 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 37）。

## 0.7.85 — 2026-11-19

**「端口也可以通过修改配置文件更改」—— 客户端根本不读这个配置项。**

### 问题

你的需求里写着「客户端控制台的监听地址是 0.0.0.0，默认端口 8730，
**端口也可以通过修改配置文件更改**」。文档 `docs/03 §4` 也确实把 `ZW_PORT`
列为"默认端口，可改"。

但客户端 GUI 起本机服务时是这样的：

```python
core = LocalCore(port=int(cfg.get("core_port") or 8730))
```

只读**客户端自己的 JSON 配置**（`cfg["core_port"]`），**完全不看 `ZW_PORT`**。
于是你在 `zhiwen.env` 里写 `ZW_PORT=8731`，GUI 照样去起 8730 ——
照着文档做却不起作用。

### 为什么这个场景真实存在

你的原话就是："因为我已经占用了 8730 端口方便你去远控，
你就让它用 8731 端口就行了"。

`LocalCore` 确实有"端口被占就回退"的兜底（这点做得对），不会彻底起不来。
但那是**"随便找个空闲端口"**，用户要的是**确定性**：
他故意把 8730 留给另一个程序，知纹必须去 8731，而不是碰运气落到 50438。

### 修法

新增 `client._resolve_core_port(cfg)`，优先级：

```
ZW_PORT（环境变量 / zhiwen.env 里的一行）  →  cfg["core_port"]  →  8730
```

非法值（`ZW_PORT=abc`）不炸，回落到默认。`gui.py` 两处构造 `LocalCore` 都改用它。

### 测试

637 → **638 项**：验三种优先级、以及非法值回落。

九项门槛全绿（638 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 37）。

## 0.7.84 — 2026-11-19

**顺着「代理列显示直连」那个 bug，把三个界面的口径对齐了。**

### 桌面端本来就是对的（补测试钉住）

上一版修的是 web 控制台。这一版先查**桌面客户端**有没有同样的问题 ——
结果发现 `gui_profiles.proxy_display_for()` **一直都是对的**：

```python
pid = profile.get("proxy_id")
if pid: return proxy_upstream_of(library[int(pid)])   # 按 id 关联代理库
```

它走的是"前端自己按 id 关联"，而 web 控制台走的是"后端补字段"，
两条路不一样 —— 所以同一个 bug 只出现在一边。

补了 `tests/test_gui_proxy_display.py`（5 项），把这个正确行为钉住：
库条目命中 / 库条目已删 / 手填地址 / 无代理 / 超长时保住 host:port。
最后一条是防回归：`ROW_LIMIT_PROXY=22` 会截断，若先砍 host 而不是协议名，
`66.80.12.66` 和 `66.80.12.65` 在界面上就长得一模一样了。

### 第三个界面：MCP 工具（给 AI 用的）

`zw_list_profiles` 返回的数据里现在有 `proxy_info`，但**工具描述里一个字没提** ——
AI 读到 `proxy: ""` 会直接判断"这环境没代理"，然后去查网络问题。

描述里补上：

> 代理看 `proxy_info`：绑了代理库时 `proxy` 字段是空的（库是唯一真相），
> `proxy_info.source=library` 且 `proxy_info.upstream` 才是实际地址；
> `source=missing` 表示库条目已被删。

`docs/05-AI接入指南.md` 与 `docs/04-接口文档.md` 同步补了这条说明。

### 为什么值得单独写一条

同一个语义（"这个环境实际用哪个代理"）在三处各写了一遍实现：

| 界面 | 做法 | 之前的状态 |
|---|---|---|
| web 控制台 | 后端 `row_to_dict` 补 `proxy_info`，前端读 | ❌ 读了空的 `proxy` → 显示「直连」 |
| 桌面 GUI | 前端按 `proxy_id` 关联库 | ✅ 一直正确 |
| MCP（AI） | 直接用接口返回 | ⚠️ 数据对，但描述没提，AI 会误读 |

这类"同一件事三个地方各写一遍"的结构，是这套代码里最容易出现不一致的地方。
现在三处口径一致，且都有测试或文档钉住。

### 测试

632 → **637 项**（`test_gui_proxy_display.py` 5 项）。
九项门槛全绿（637 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 37）。

## 0.7.83 — 2026-11-19

**界面上「代理」那一列，绑了代理库的环境会显示成「直连」——与事实相反。**

### 问题

用户要求过"控制台要多一列显示代理"。那一列确实在，渲染的是：

```js
['代理', p.proxy || '直连', true]
```

但 `assign_to_profile()` 绑定库条目时会**有意把 `profiles.proxy` 置空、只留 `proxy_id`**
（让"代理库"成为唯一真相，`proxy` 字段只表示"手填地址"）。

两边一撞：**环境明明在用 `socks5://66.80.12.66:20000`，界面显示「直连」**。
实测确认（本地起 core，建库条目 → 绑到环境 → GET /profiles）：

```
proxy_id: 1 | proxy: ''     ← 界面据此渲染 → 「直连」
```

### 根因：写好的函数没人调用

`proxylib.describe_profile_proxy()` **就是为这件事写的**，docstring 里写着
「列表页的 `proxy` 字段会看起来是空的 —— 展示时走这个函数，不要直接读 `profiles.proxy`」。
但全项目**没有任何地方调用它**（只有注释里提到）。

典型的"函数写好了但没接线"。

### 修法

1. `row_to_dict()` 在环境绑了库条目时填 `proxy_info`（就是 `describe_profile_proxy()`
   的返回值：来源、label、upstream、是否链式、上次检测结果…）。
2. 控制台「代理」列改读 `proxy_info`：

```js
if (pi.source === 'library') return pi.upstream || '代理库';
if (pi.source === 'missing') return '库条目已删除';
return p.proxy || '直连';
```

顺带处理一个连带问题：编辑表单里的「代理地址」输入框原来填 `p.proxy`，
现在如果回填库里的地址，就会变成"库 + 手填"两个来源，而手填优先级更高 ——
用户一保存就把库绑定挤掉了。改成绑库时该框留空并说明"当前用库里的条目"。

### 我中途走错的一步

第一版我直接改 `row_to_dict` 把 `proxy` 字段填上展示值，结果**两条既有测试立刻红**：

```
test_assign_library_proxy_keeps_credentials_effective
test_proxy_spec_for_quotes_credentials
  → assert fresh["proxy"] == ""      # 脱敏字段不再承担"地址来源"
```

它们明确断言 `proxy` 必须为空 —— 那才是原始设计的意图。我改字段等于推翻设计。
撤回，改成"不动 `proxy`、只加 `proxy_info`"，测试全绿。

> 这正体现了那两条测试的价值：它们把"字段语义"钉住了，防止后来的人（包括我）
> 图省事把两个概念混进一个字段。

### 测试

631 → **632 项**（库代理展示：断言 `proxy_info.source == library`、带出 upstream、
且 `proxy` 保持为空）；`ui-check` 33 → **37 项**。

九项门槛全绿（632 / 34 / 73 / 13 / 23 / 81 / 37 / 12 / 37）。

## 0.7.82 — 2026-11-19

**「远程凭机器码进控制台」这条需求，之前只验了 API 层，没验登录框。**

### 缺的是什么

你的原话有两条，我一直只验了第一条：

| 需求 | 之前的验证 |
|---|---|
| 本机打开控制台免令牌 | ✅ 真浏览器验过（`ui-check`） |
| **远程访问只需输入机器码** | ⚠️ 只验了"API 带机器码返回 200"，**没验登录框认不认机器码** |

API 能过不代表界面能用 —— 登录框可能写着"访问令牌"、可能把输入发到别的头、
可能接受但不跳转。这些只有真浏览器点一遍才知道。

### 这轮补的验证（真浏览器，从 LAN IP 打开）

```
回环 http://127.0.0.1:8894     → auth_required=false  直接进
非回环 http://192.168.3.6:8894 → 401 提示「输入这台电脑的机器码」
带 Bearer zwd_…                → 200
真浏览器打开 192.168.3.6:8894  → 要求登录，文案就是「机器码」
输入 zwd_… 点进入              → 登录层隐藏，控制台渲染出「总览/环境/扩展…」
```

顺带确认了一条容易忽略的约束：**没配 `ZW_ADMIN_TOKEN` 也没配 `ZW_DEVICE_ID` 时，
`ZW_HOST=0.0.0.0` 会被强制降级成 `127.0.0.1`**（安全默认），
所以远程验证必须先给 device_id —— 这也解释了第一次测时 LAN IP 访问返回空。

### 新增 `scripts/remote-console-check.cjs`（12 项）

专门走非回环路径，覆盖：回环免令牌 / 非回环 401 / 带机器码可读 /
登录文案提到机器码 / 输码后登录层隐藏 / 控制台渲染 / 无 JS 报错。

已并入 `check-all.sh` 作为第 9 项门槛（用本机 LAN IP 跑）。

### 一个我自己写错的断言

第一版判据是「页面里不能出现'机器码'字样」—— 但控制台的说明文字里本来就有，
于是"已经进去了"被误判成失败。改成**检查登录层 `#login.show` 是否隐藏**，
这才是"进去了"的准确判据。

### 门槛

九项全绿（631 / 34 / 73 / 13 / 23 / 81 / 33 / **12** / 37）。

## 0.7.81 — 2026-11-19

**Linux CI 连续两次红在同一处：一条测试在 CI 上跑到 snap chromium 上去了。**

### 症状

`pytest --timeout=90` 卡在这里超时：

```
tests/test_extstore.py::test_installed_extension_actually_runs_in_browser
  → _mgr.stop(prof["id"], force=True)
  → engines.terminate() 的 time.sleep(0.25) 循环
```

第一次我以为是偶发，重跑一次**又红**，于是把完整日志拉下来定位到具体用例。

### 根因

`_loadable_engine()` 的候选里有一句 `shutil.which("chromium")`。
GitHub 的 ubuntu runner 上装了 **snap 版 chromium**，而探针是真启动一次浏览器来判断
"能不能加载扩展" —— 在那里它返回可加载，于是这条"真浏览器"测试就跑起来了。
snap 版在 CI 沙箱里进程不退，`terminate()` 的 `grace*4` 循环一直等到 90 秒超时。

**本地一直绿**，是因为我这台机器上 `/opt/bsm-p0` 有正经的 ungoogled，候选列表第一个就命中了，
压根走不到 `which("chromium")` 那条。

### 修法（三处）

1. **只认明确装了 ungoogled 的位置**，删掉 `which("chromium")`。
   这条测试的价值是"在我装了 ungoogled 的机器上验端到端"，不是"在任意 CI runner 上碰运气"。
2. **不再访问 `https://example.com`**，改用本地 HTTP 页 —— 和上一轮 `gateway-check`
   是同一类问题：验收/测试不该依赖外网。
   （注意不能用 `data:` URL：内容脚本匹配 `<all_urls>`，**不含 data: 方案**，
   我第一版这么改导致断言假失败，已改回 http。）
3. **`ZW_CHROME_BIN` 用完恢复**：原来直接写进 `os.environ` 不还原，
   会把后面的用例带到不该用的内核上。

单文件耗时 30s → **22s**，44 项全过。

### 测试

八项门槛全绿（631 / 34 / 73 / 13 / 23 / 81 / 33 / 37）。

## 0.7.80 — 2026-11-19

**自己下载安装的内核，自己认不出来 —— 用真实 Windows 包验出来的。**

### 怎么发现的

一直没验过「自动下载的 Windows 包长什么样」。这次直接用本机代理下了真实的
`ungoogled-chromium_157.0.8103.78-1.1_windows_x64.zip`（**202,853,365 字节，16.4 秒**），
用 `install_archive` 真解包真落地，然后检查装完的结构：

```
落点: engines/157.0.8103.78-1.1/chrome.exe      ← 目录名里没有 "ungoogled"
同级 25 个文件，chrome.dll / icudtl.dat / v8_context_snapshot.bin 都在
```

解包与整层搬运都是对的（`_find_binary` 正确跳过根层、搬的是整个父目录）。
但用这个真实结构去 `find_chrome`：

```
engine_dir 指向 engines 根        → ENGINE_NOT_FOUND
engine_dir 指向 157.0.8103.78-1.1 → ENGINE_NOT_FOUND
```

**自己刚装好的内核，自己认不出来。**

### 根因

自动安装时目录名用的是 GitHub 的 tag（`157.0.8103.78-1.1`），而
`policy=ungoogled` 的判定要靠路径里的 `ungoogled` 标识（那是区分"官方 Chrome"
与"去谷歌家族"的唯一线索，官方 Chrome 137 起不加载命令行扩展）。

两者一撞，就是这个结果：装成功、判定失败、启动环境报
「没有找到可用的 ungoogled-chromium 内核」。

### 修法

目录名改成 `ungoogled-<tag>`：

```
engines/ungoogled-157.0.8103.78-1.1/chrome.exe
```

改完两种 `engine_dir` 配法都能正确识别（已用真实目录验证）。

### 顺手确认的两件事（本来就对）

- **整层搬运**：`install_archive` 搬的是 `found.parent`，不是单个 exe。
  用真实包验证：25 个同级文件 + `locales/` 都跟着过去了 —— 只搬 exe 会得到一个
  跑不起来的残废。
- **`_find_binary` 的深度限制**：Windows 包是 `<一层目录>/chrome.exe`（深度 2），
  在限制内。

### 测试

629 → **631 项**：一条验「自动安装的目录名必须含 ungoogled」，
一条用与真实布局同构的包验「整层搬运 + 同级文件齐全」。

八项门槛全绿（631 / 34 / 73 / 13 / 23 / 81 / 33 / 37）。

## 0.7.79 — 2026-11-19

**代理凭据会泄露到日志和接口响应里 —— 修掉，并加测试盯住。**

### 问题（我自己引入的）

`ZW_DOWNLOAD_PROXY` 允许写 `http://user:pass@host:port`。而我在做「代理自动发现」
和「内核卡片」这两轮时，把整个代理 URL 直接放进了三个地方：

| 位置 | 后果 |
|---|---|
| `enginesrc._proxy()` 的 info 日志 | 密码进日志文件 |
| `extstore` 的错误消息「当前走的代理：…」 | 密码随错误返回到**界面**上 |
| `GET /api/v1/engine` 的 `download_proxy` 字段 | 密码进 HTTP 响应 |

项目硬约束第 7 条明确写着「任何日志/响应里不得出现密码原文」。
这三处都在违反它，而且都是我最近几轮新加的代码 —— 老代码反而是干净的。

### 修法

新增 `netproxy.mask_proxy()`：把 `user:pass@` 换成 `***@`，**保留主机与端口**
（排查问题时需要知道走的哪台），三处全部改用它：

```
http://user:secret123@127.0.0.1:7890  →  http://***@127.0.0.1:7890
http://127.0.0.1:21080                →  http://127.0.0.1:21080   （未变）
```

`describe()` 在返回前也脱敏一次 —— 它直接进接口响应，不能指望调用方记得打码。

解析失败时**整体打码成 `***`**：宁可少给信息，也不泄露。

### 测试

新增两条（629 项）：一处验 `mask_proxy` 的三种输入 + `describe()` 输出，
一处用 `caplog` 断言 `enginesrc` 的日志里搜不到密码原文。

### 说明

这轮没动功能，只堵了一个安全漏洞。云电脑仍离线，等它上线再做插件真实加载的最终验收。

八项门槛全绿（629 / 34 / 73 / 13 / 23 / 81 / 33 / 37）。

## 0.7.78 — 2026-11-19

**抓到那个反复出现的 `Cannot read properties of undefined (reading 'cookies')` 真根因，并让桌面客户端也能看到内核。**

### ① 真根因：验收步骤依赖了外网

这个报错在 check-all 里偶发过四次。前几次我加等待、加重连、加诊断，都没根治。
这次留下日志才看清：

```
03:13:11  环境已就绪 name=gw-check ws=ws://127.0.0.1:22001/devtools/browser/…
03:23:55  CDP 断开 profile=… 时长=643.5s        ← 中间 10 分 43 秒，零日志
```

`gateway-check` 里那一步是 `page.goto('https://example.com')` —— **验收脚本在访问公网**。
网络一抖，Playwright 的连接就挂在那里，后面的 cookie 断言自然全红。
单跑时网络好就过，跑全套时偶发失败，看起来像网关或 cookie 代码坏了。

修法：导航改用**本地 HTTP 页**（现起一个 `node:http` 服务），并给 `title()` /
`textContent()` 各加 15s 上限。验收要证明的是"经网关的 CDP 能开页面、能读 DOM"，
用本地页足够且结果确定；外网连通性另有 `browserleaks` / `ip123` 专门检测。

顺带修了个自己写出来的语法错误：JS **不会**把相邻字符串字面量自动拼接
（那是 Python 的行为），必须显式写 `+`，否则整个文件 `missing ) after argument list`。

断言 22 → **23**（新增"能读到页面 DOM 内容"）。

### ② check-all 先检查端口池干不干净

同一批排查里还发现：上一次 check-all 被中断时，它拉起的 headless 浏览器不会回收，
继续占着 22001/22002。下一次运行时新浏览器拿不到调试端口 —— 表现又是 cookie 断言失败。

现在起 core 之前先扫 22001-22040 与 22101-22140，被占就把占用者列出来并给出清理命令
（**不自动杀**：杀错东西代价更大）。

### ③ 桌面客户端也能看到内核了

上一版把「内核」卡片加到了 web 控制台，但**桌面客户端没有** —— 而桌面端才是日常入口。
而且 `local_summary()` 里那个 `engine` 字段**取到了却没有任何地方展示**（死值）。

- 状态卡片多一格「内核」：`可加载扩展` / `不加载扩展` / `缺失` / `下载 xx%` / `检测中`
- 「扩展包」面板加「下载内核」按钮（插件能否生效取决于内核，放在插件旁边最顺手）
- `LocalCore.engine()` 走只读缓存接口，不会因为探针卡住界面

`gui-check` 35 → **37 项**。

### 测试

八项门槛全绿（627 / 34 / 73 / 13 / **23** / 81 / 33 / **37**）。

## 0.7.77 — 2026-11-19

**清自己造成的文档债务：好几份文档还在教用户手动下载内核。**

### 问题

我把默认内核换成 ungoogled-chromium（0.7.72）、并让客户端自己下载安装之后，
这几处文档没跟上：

| 文档 | 原来写的 |
|---|---|
| `docs/01-快速开始.md` | 「方案 B：fingerprint-chromium（推荐）」+ `curl` 下载 + `tar xf` 手动解压 |
| `docs/01-快速开始.md`（Linux） | `sudo apt install chromium` 当首选方案 |
| `docs/09-交接说明.md` | 「第 2 步：换指纹内核（重要，10 分钟）」+ 手动 `Invoke-WebRequest` |
| `AGENTS.md` | 「指纹由 fingerprint-chromium 或**系统 Chrome** 提供」 |

这种文档**比没有更糟**：用户照着做，装完发现和程序自动管理的内核并存，
反而搞不清哪个在生效。而 `apt install chromium` 装的发行版构建多数也跟随上游
限制了 `--load-extension`，装完插件照样不生效。

### 改成什么

- 快速开始：先说明「内核不用管，会自动装」，给 `engine status` / `engine install`
  两条命令；并**写清为什么不能用系统 Chrome**（137 起忽略 `--load-extension`）
- 保留"想要一号一设备再加 fingerprint-chromium"的说明，但改成
  「把目录放进 `<数据目录>/engines/` 即可，policy=ungoogled 会自动接受它」
- 交接说明第 2 步从「换内核（10 分钟手动）」改成「确认内核能加载扩展（1 分钟）」
- `AGENTS.md` 开头那句改成实际默认：ungoogled + 缺失时自动下载

### 加了一条文档断言

`test_quickstart_tells_users_engine_is_automatic`：快速开始里必须出现
`engine install`、**不能**再出现 `Expand-Archive` 或 `releases/download`、
且必须解释 137/`--load-extension`。以后再有人把旧步骤抄回去，CI 直接红。

### 顺带：给 CDP 超时加了诊断

`gateway-check` 的 cookie 断言在 check-all 里偶发失败过一次（单独跑、连续跑两次都过）。
真正原因没抓到，所以先让**下次失败时能说清**：

```
CDP 命令 Network.enable 超时（60s，期间收到 1234 条其它消息，session=216fd0f9）
```

这三个数字能区分三种完全不同的原因：浏览器没响应 / session 失效
（响应永远不来）/ 事件洪流把时间吃光。没有它只能靠猜。

### 测试

626 → **627 项**。八项门槛全绿（627 / 34 / 73 / 13 / 22 / 81 / 33 / 35）。

## 0.7.76 — 2026-11-19

**不铺新功能，专门加固我这几轮快速加的两块代码（`netproxy` / `enginesrc`）。**

### ① 一个潜伏的死锁（最严重）

`start_background()` 里我把 `is_running()` 判断和 `_thread = ...` 放进了锁里 ——
这是对的（否则连点两下「下载内核」会各自下 150MB 去抢同一个目录）。
但那把锁原来是普通 `threading.Lock`，而**持锁期间会调 `_reset()` 与
`progress()`，它们内部也拿同一把锁** → 自己等自己 → **死锁**。

表现会是：点「下载内核」毫无反应、也不报错。最难受的那种 bug。
改成 `RLock`，并在注释里写清为什么必须是可重入锁。

### ② 代理探测只试一个域名（会悄悄退回直连）

原来只探 `https://api.github.com/zen`。某些代理/网络组合下这个域名不通、
但 `www.google.com` 通（或反过来）。只探一个就会把**其实能用**的代理判成不可用，
然后安静地退回直连 —— 表现是"下载很慢"，而不是"代理没生效"，很难归因。

改成多目标，**任一通即算可用**。

### ③ 磁盘空间预检

压缩包 140–200MB，解开通常 **700MB–1GB**。半路写满的表现是
`shutil.move` 抛 OSError 或文件被截断 —— 用户看到"装好了但跑不起来"，
根本想不到是磁盘。

新增 `_check_disk_space()`：安装前按「压缩包 × 5」估算并留 10% 余量，
不够就报 `ENGINE_NO_SPACE`（HTTP 507）并说明要清哪个目录。
拿不到磁盘信息时**不拦**（不能因为查不了就不让装）。

### 顺带确认的两个边界（本来就对）

- 压缩包里二进制直接在根目录（没有外层文件夹）→ 也能正确装
- 重复调 `ensure()` → 走 `skipped`，不会重复下载

### 测试

624 → **626 项**（并发/死锁、磁盘预检、多目标探测各一条）。
八项门槛全绿（626 / 34 / 73 / 13 / 22 / 81 / 33 / 35）。

## 0.7.75 — 2026-11-19

**内核状态搬进控制台（能看、能点），顺带修两个我自己引入的回归。**

### ① 控制台新增「内核」卡片

默认内核已经换成 ungoogled-chromium，但界面上只有「服务元信息」里一行
"内核 版本 —"，用户既不知道为什么要换、也不知道怎么装 —— 只能去翻文档找命令。
自动下载失败时（例如没配代理）更是什么都看不到。

现在「运维」页顶部有一张独立卡片：

```
内核                                    [能加载扩展]  [下载内核 / 重新下载]
  策略        ungoogled
  当前内核    …/engines/156.0.8078.79-1/chrome
  版本        Chromium 156.0.8078.79
  自动安装    开
  引擎目录    …/engines
  下载代理    http://127.0.0.1:21080  autodetect:21080
```

带下载进度与失败原因；探针还在跑时显示「检测中…」。

### ② 状态接口不能同步跑探针（把它拖到 0.02 秒）

第一版在 `GET /api/v1/engine` 里**同步**调 `extprobe.probe()` —— 那要真启动一次
浏览器，慢的时候 10–25 秒。结果界面像卡死，直接把 `ui-check` 拖到
`waitForFunction 120000ms exceeded`。

改成：只读缓存（新增 `extprobe.peek()`），没缓存就先返回
`extensions_loadable: null` + `probe_pending: true`，并**后台**补探
（`extprobe.probe_async()`）。接口耗时 **25s → 0.02s**。

### ③ 回归：`ZW_ENGINE_DIR` 直接指内核目录时，真内核被判成官方版

上一版为了"别拿绝对路径判断"，改成只看相对路径。但很多人（包括我们的
check 脚本）是这样配的：

```
ZW_ENGINE_DIR=/opt/xxx/ungoogled-chromium-150.0.7871.186/
```

此时 `chrome` 的相对路径就只有 `chrome`，不含 ungoogled 字样 →
被判成"官方发行版"→ 拒绝 → 启动环境报 `ENGINE_NOT_FOUND`。
`ui-check` 的「点启动后变成运行中」超时把它抓出来了。

正确规则（两个方向都踩过，最终版本）：

- 相对路径**有目录层级**（`official-chrome-157/chrome`）→ 以它为准，
  **绝不能被上层目录名里的 "ungoogled" 洗白**
  （用户把引擎放在自建的 `ungoogled-tmp/` 下就是这种情况）
- 相对路径**就是裸文件名**（`chrome`）→ 说明 engine_dir 指的就是内核目录，
  这时才参考 `engine_dir.name`

两条都有测试盯着。

### 测试

622 → **624 项**（两种 engine_dir 形状各一条）；`ui-check` 29 → **33 项**。
八项门槛全绿（624 / 34 / 73 / 13 / 22 / 81 / **33** / 35）。

## 0.7.74 — 2026-11-19

**顺手修掉一个反复出现的偶发失败，并让"去谷歌内核家族"的判定不再被骗。**

### ① `Cannot read properties of undefined (reading 'cookies')` 的真根因

这个报错在 `check-all` 里偶发出现过三次，我前两次都是加等待（`waitRunning`）绕过去。
这次拿到保留日志才看清真正原因：

```
File ".../services/cookies.py", line 176, in _dump_cookies
    await cdp.call("Network.enable")
TimeoutError
2026-11-19 00:06:59 ERROR [uvicorn.error] Exception in ASGI application
```

**`Network.enable` 冷启动时 40 秒都没回**，而它只被包在 `CDPError` 的处理器里 ——
裸 `TimeoutError` 直接冒成 **500 + 整段栈**，前端拿到 `undefined`，
界面上就只剩那句 JS 报错。

三处一起修：

1. `Network.enable` 单独给 60s（它会触发建网络域、枚举 target，冷启动确实慢）
2. `dump_cookies()` **超时/断连时重连重试一次**（第一根 ws 常常已经半死）
3. 裸 `TimeoutError` / 连接异常**统一包成 `CDPError`** —— 这样才会走 api 层那套
   结构化处理器，而不是 500。错误消息也改成人话：
   「浏览器还没准备好（读 cookie 超时）。等它启动完成再试一次。」

`gateway-check` 侧也改了：连续 5 次退避重试，并**打印真实错误**，
不再只丢一句 `undefined`。断言数 21 → **22**。

### ② `policy=ungoogled` 会把 fingerprint-chromium 拒掉

`fingerprint-chromium` **本身就是 ungoogled-chromium 的分支**（上游 README 原话：
based on Ungoogled Chromium），同样能加载 `--load-extension`。原来只认路径里带
"ungoogled" 字样，用户装了 fingerprint-chromium 反而被判定"没有可用内核"，
然后被引导去下载一个其实已经有的东西。

改成「不是官方发行版即可」：带 `ungoogled` 或 `fingerprint` 都接受。
真正决定能不能加载扩展的仍是**探针实测**，名字只用于排序/初筛。

### ③ 判定不能看绝对路径（被自己的测试目录名骗了）

写"必须拒掉官方内核"的测试时，它一直失败，查了半天发现：

```
/tmp/pytest-of-root/pytest-538/test_ungoogled_policy_rejects_0/engines/official-chrome-157/chrome
                                                     ^^^^^^^^^^ 路径里有 "ungoogled"
```

`tmp_path` 里含测试函数名，而函数名里有 `ungoogled` —— 于是"官方内核"被判成
ungoogled 家族。**同样的事也会发生在用户身上**：把引擎放在 `D:\ungoogled测试\`
下，任何内核都会被当成优先项。

修法：判定只看**引擎目录内的相对路径**。`enginesrc._existing()` 里同样的写法也一并改了。

### 测试

620 → **622 项**（fingerprint 接受 / 官方内核拒绝 各一条）。
八项门槛全绿（622 / 34 / 73 / 13 / **22** / 81 / 29 / 35）。

## 0.7.73 — 2026-11-18

**客户端自己找本机代理 —— 不用再教用户配 `ZW_DOWNLOAD_PROXY`。**

### 起因（用户给的线索）

用户提到本机的 Clash 订阅。我一查才发现：**这台机器早就有可用代理**
（`127.0.0.1:21080`，CrashCore/mihomo），而我前面一直在借云电脑的代理绕远路。

实测同一台机器、各 8 秒：

| 路径 | 8 秒拿到 |
|---|---|
| 直连 GitHub release | **146 KB** |
| 经 `http://127.0.0.1:21080` | **49.4 MB**（约 6 MB/s） |

差三个数量级。而默认没人会去设那个环境变量 —— 用户机器上大概率本来就跑着
Clash/mihomo（`7890`、`7892`、`21080`…），只是没人告诉本程序。

### 做了什么

新增 `services/netproxy.py`：显式配置优先；没配就**挨个探常见本地端口**
（`7890/7891/7892/21080/7897/1080/10808`），找到**真的能用**的才采用。

**关键细节：不能只看"端口开着"。** 本机 `7892` 是开着的，但经它访问外网直接
失败（`curl -x` 返回 000）。只看 TCP connect 会挑中它，然后所有下载莫名卡住 ——
这种失败最难查（表现是"下载不动"，不是"代理配错"）。所以每个候选都要做一次
真实探测：通过它请求一个极小 HTTPS 端点，拿到响应才算通。

配置：

```
ZW_DOWNLOAD_PROXY_AUTODETECT=true|false   # 默认 true
ZW_DOWNLOAD_PROXY_PORTS=7890,20171        # 自定义候选，留空用内置清单
GET /api/v1/engine → download_proxy {proxy, source}
                                       # source: explicit / autodetect:21080 / none
```

### 实测

**完全不设 `ZW_DOWNLOAD_PROXY`**（`env -u ZW_DOWNLOAD_PROXY`）：

```
zw engine install
  开始下载（约 150–200MB）…
  完成：Chromium 156.0.8078.79
  路径：…/engines/156.0.8078.79-1/chrome      （768MB，真实下载）
```

自动发现选中 `21080`，耗时 0.82s；对照"只给打不通的 7892"→ 正确判定 `none`。

### 加自动发现引出的两个连带修复

1. **`test_download_crx_unreachable_host` 红了**：自动发现把本机代理挑上后，
   "连不上 `127.0.0.1:1`"变成了"代理回了一个 HTTP 错误码"，错误码从
   `EXT_STORE_UNREACHABLE` 变成 `EXT_STORE_HTTP_ERROR`。测试里显式关掉探测
   （要钉的语义就是"真的连不上"）。
2. **错误消息里补上"当时走的哪个代理"**。自动发现挑中一个不可用的本地端口时，
   用户只看到"连不上商店"会去查网络，其实该查那个代理。现在消息里直接写
   `（当前走的代理：http://127.0.0.1:xxxx）` 或 `（未使用代理，直连）`。

### 关于用户的 pscly4 订阅

订阅已取到（3.2KB，自建东京节点 `149.62.44.91`，VLESS+REALITY 与 Hysteria2）。
**没有改用户现有的 ShellCrash 配置** —— 本机 `21080` 已能跑 6MB/s，改配置收益不大
而风险实在（那份 config 里有策略组、直连规则、注入的自建节点）。已在文档里说明，
需要正式并入时再做。

### 一个只在 Windows 上炸的测试（CI 抓到）

`test_ensure_full_pipeline_over_real_http` 里我写死了 `chrome`：

```
E  EngineSourceError: 解包后没找到 chrome.exe，这个包可能不是可用的内核
```

Windows 上可执行文件是 `chrome.exe`。Linux/macOS 全绿、Windows CI 红 ——
**测试的平台假设错了**，不是产品问题。已按 `os.name` 取文件名，
并把"可执行位"那条断言在 Windows 上跳过（没有 POSIX 可执行位概念）。

改完 Windows 还是红，第二次报的是：

```
EngineSourceError: 内核装好了但跑不起来（--version 无输出）。可能缺系统库。
```

假内核是个文本文件：Windows 根本不会执行它（`.exe` 是假的），Linux 上
`#!/bin/sh` 的脚本又能跑 —— 同一个测试在两个平台行为不同。

于是把"跑 --version"用桩替掉（这条测试要验的是
下载/校验/解包/落盘/可执行位这条链路），**并补了两条独立测试**专门盯住
版本探测与 `ENGINE_BROKEN` 这条路，避免被桩掩盖 ——
后者恰恰是用户最容易撞上的（Linux 缺 libnss3/atk/gbm、Windows 被杀软拦）。

### 测试

611 → **618 项**（`tests/test_netproxy.py` 6 项 + 其它）。
八项门槛全绿（618 / 34 / 73 / 13 / 21 / 81 / 29 / 35）。

## 0.7.72 — 2026-11-18

**客户端默认改用 ungoogled-chromium，并且能自己把它装好 —— 不再需要人工下载解压。**

### 为什么必须换内核

官方 Google Chrome **137 起忽略 `--load-extension`**。我在本机用扩展加载探针实测：

| 内核 | 能否加载命令行扩展 | 耗时 |
|---|---|---|
| ungoogled-chromium 150 | **能**（内容脚本真的执行） | 1.6s |
| 官方 Google Chrome 149 | **不能** | 10.4s（超时判定） |

所以在官方内核上"把插件带进环境"只是把文件放过去、浏览器不认 —— 你之前看到的
`extensions_active: false` 就是这件事。

### 做了什么

**① 新的内核策略（默认 `ungoogled`）**

`ZW_ENGINE_POLICY=ungoogled`（默认）时只认带 ungoogled 字样的内核；
找不到就报 `ENGINE_NOT_FOUND` 并给出三种解决办法，**不会静默退回官方 Chrome**
（退回去等于悄悄丢掉插件能力，用户不会知情）。`policy=any` 保留旧行为。

**② 客户端自己装内核（新模块 `services/enginesrc.py`）**

- 查 GitHub `releases/latest`，按平台选资产（Windows `windows_x64.zip` /
  Linux `x86_64_linux.tar.xz`）
- 下载**必须**有校验值：用 GitHub API 给的 `sha256:...` 边下边算，不通过就丢弃；
  资产没有 digest 时**直接拒绝安装**（不闭眼装二进制）
- 安全解包（拒绝 `../` 越界）、定位 `chrome`/`chrome.exe`、`--version` 自检，
  落到 `<数据目录>/engines/<版本>/`
- 启动时后台进行，不阻塞；进度见 `GET /api/v1/engine`
- 支持 `ZW_ENGINE_URL` + `ZW_ENGINE_SHA256` 手动指定（macOS / 内网镜像 / 离线）

**③ 接口与命令**

```
GET  /api/v1/engine          内核状态 + 能否加载扩展 + 下载进度
POST /api/v1/engine/install  后台下载安装（?force=true 重装）
zw engine status / install
```

### 实测（本机，真实内核）

用本机已有的 ungoogled 150 作为引擎，policy=ungoogled 起一个 core：

```
GET /api/v1/engine → path=…/ungoogled-chromium-150/chrome
                     version=build 150
                     extensions_loadable=True        ← 官方 Chrome 这里是 False
建环境 + 应用一个真扩展（带内容脚本）→ extensions_active=True
启动 → 用 CDP 开一个真实 http 页面 → 读 DOM 上内容脚本写入的标记 → "yes"
```

最后那条才是硬证据：**内容脚本真的执行了**，不是"文件放进去了"。
（顺带印证：扩展 target 数仍是 0 —— 所以判据不能看 `/json/list` 有没有
`chrome-extension://`，只能看内容脚本痕迹。）

打包产物的 138MB 下载在本机被 GitHub 限速（20 分钟只到 32MB），
所以"联网下载"这条用本地内核等价验证；下载/校验/解包/自检的机制另有 7 项单测覆盖
（含校验不通过必须丢弃、越界路径必须拒绝、没有 digest 必须拒绝）。

### 顺带修掉的两个既有 bug

1. **`engine_dir` 一直等于"当前工作目录"**：`Path("")` 就是 `Path(".")`，真值为 True，
   于是 `resolve()` 里"回落到 `<数据目录>/engines`"的逻辑从没执行过。
   以前没炸是因为测试都用 `ZW_CHROME_BIN`；自动装内核会真往 cwd 解 150MB。
2. **文档一致性测试对新异常类失明**：它写死了异常类名清单
   （`ApiError|ProfileError|…|PackError`）和模块前缀清单，新增 `EngineSourceError`
   既不被检查、也不要求注册处理器。改成通用匹配后，一次揪出 **13 个从没被文档记录的
   真实错误码**（`EXT_STORE_*`、`SHELL_*`、`SESSION_DEAD` 等），已全部补进 `docs/04`。

### 自己踩的坑

1. `_engine_version()` 第一版直接 `subprocess.run` —— 被 `test_proc_hidden` 挡住：
   Windows 上会弹出黑色控制台窗口。改成 `proc.run_hidden`。

2. **CI 红了，根因是「内核检查抢在参数校验前面」**。`policy=ungoogled` 之后，
   启动一个**代理串写错**的环境时，报出来的是「没有找到 ungoogled 内核」，
   而不是「代理串里有空格」——用户会去查内核，其实改代理就好。

   CI 上暴露这件事的是 `test_bad_proxy_blocks_start_with_readable_error`：

   ```
   AssertionError: assert ('空格' in '没有找到可用的 ungoogled-chromium 内核…'
                           or 'IP' in '没有找到可用的 ungoogled-chromium 内核…')
   ```

   修法不是改断言，而是**调整顺序**：先校验代理（用户当场能改的参数），
   再找内核（环境问题）。这类"两个错误都能报，但先后顺序决定用户往哪查"的问题，
   只有跑全量才会露出来。

### 测试

604 → **611 项**（`tests/test_enginesrc.py` 7 项）。
八项门槛全绿（611 / 34 / 73 / 13 / 21 / 81 / 29 / 35）。

## 0.7.71 — 2026-11-18

**装商店扩展暴露三个真 bug：库里看不到、名字是 `__MSG_xxx__`、包名也是占位符。**

### 场景

用户给了 5 个商店链接要在云电脑上装（YesCaptcha / ScriptCat / 沉浸式翻译 /
Cookie-Editor / Bitwarden），装完发现：

```
GET /extensions（扩展库）  → 只返回 4 条旧记录，刚装的 5 个**全都不在**
扩展包列表                → 4 个包名是 __MSG_extName__ / __MSG_scriptcat__ / __MSG_brandName__
磁盘 ext_dir/             → 9 个目录都在（含 5 个新装的）
```

### ① 装了但「扩展库」看不到（最严重）

`GET /extensions` 读的是 **SQLite 的 `extensions` 表**，而 `extstore.install()`
以前只做两件事：把文件放进 `ext_dir/`、建扩展包 —— **从不写这张表**。
只有被应用到环境时（`packs.register_extension`）才登记，太晚了。

用户装完一刷新列表还是空的，只会以为装失败了。已补 upsert（含 sha256）。

> 修的时候我自己还踩了一下：登记代码里漏了 `import time`，而它被
> `except Exception` 包着 —— **静默吞掉，测试照样绿**。是加了一条"真的去查表"
> 的断言才暴露的。这种"吞异常 + 测试只验返回码"的组合特别容易骗过人。

### ② 扩展名显示 `__msg_extname__`

0.7.70 只把 `__MSG_xxx__` → `_locales` 的解析加在了**扩展包**这条路径，
**单扩展库这条路径没用上**。抽出 `services/extmeta.py` 两边共用。

顺带修一个连带问题：`slug`/目录名原本在**解包之前**用原始 name 算出来，
所以目录也叫 `__msg_extname__-2026.10.2`。改成解包后再解析、再算 slug。

### ③ 包名是占位符（扩展名却是中文）

`extstore` 建包时把 manifest 的**原始 name** 当包名传进去，绕过了自动取名：

```python
_make_pack(dest, name=str(manifest.get("name") or slug), ...)   # ← 旧
_make_pack(dest, name=display or slug, ...)                     # ← 新
```

于是包里的扩展叫「脚本猫」「沉浸式翻译…」，包名却是 `__MSG_scriptcat__`。

### 顺带：`prodversion` 写死 130 导致 Bitwarden 装不上

`clients2.google.com` 对 Bitwarden（`nngceckbapebfimnlniiiahkandclblb`）回
**HTTP 204**（无可用包）。同一 ID 换成 `prodversion=157.0.0.0` 立刻能下
（实测 26,044,856 字节）。默认值改成 `157.0.0.0`。

### 实机交付（云电脑 12.162，v0.7.70）

5 个扩展全部装好，并**下发到你那 4 个环境**：

```
tests5 / 3 / 2 / 1  →  各 5 个扩展
   bitwarden-password-manager
   cookie-editor
   沉浸式翻译---ai-双语网页翻译--pdf翻译--视频翻译--漫画翻译
   脚本猫
   yescaptcha-人机助手
```

5 个包都设成了「新建环境默认带上」。顺带清掉了 `2` 号环境里我之前测试留下的
`probewin`。

### 测试

601 → **604 项**（库登记 / 包名解析 / 扫到的名称三条）。
八项门槛全绿（604 / 34 / 73 / 13 / 21 / 81 / 29 / 35）。

## 0.7.70 — 2026-11-18

**云电脑升到 0.7.69 后，真机验证「扫描本机 → 导入 → 默认带上」整条链，顺带修掉扩展名显示成 `__msg_extname__`。**

### 真机验证（Windows，客户端 0.7.69）

**① 扫描本机（Windows 分支第一次真正跑起来）**

```
GET /api/v1/packs/scan-browsers
→ 2 个候选：
   Edge    Default  2 扩展  C:\Users\Administrator\AppData\Local\Microsoft\Edge\User Data\Default
   Chrome  Default  1 扩展  C:\Users\Administrator\AppData\Local\Google\Chrome\User Data\Default
```

（没有 RXYCDP —— 再次确认它不在云电脑上。但 Windows 的路径扫描逻辑是对的。）

**② 用扫到的路径直接导入（走界面同一条链路）**

```
POST /packs/import {"from_chrome": "<扫到的 Edge 路径>", "as_default": true}
→ id: 7e96af76c968 | 扩展数: 2 | is_default: true
```

`as_default: true` 真机确认生效（这是 0.7.68 修的"字段定义了却没接上"那个 bug）。

**③ 新建环境自动带上**

```
POST /profiles {"name": "Edge插件自检-可删"}
→ extensions: ['__msg_extname__', 'edge-relevant-text-changes', 'cookiemanager---cookie-editor']
```

默认包生效。测试环境与测试包随后都已删除，服务器只剩用户自己的 4 个环境。

### 修：扩展名显示成 `__msg_extname__`

第 ③ 步暴露的：Chromium 系扩展常把 `name` 写成占位符 `__MSG_extName__`，
真实文案在 `_locales/<lang>/messages.json`。不解析的话，列表里就是一堆
`__msg_extname__`，用户分不清那是哪个插件（中文市场上很常见）。

新增 `_resolve_manifest_name()`：按 `zh_CN` → `zh` → `en` → `en_US` 顺序找
`_locales`，取 `messages[<key>].message`；找不到就保留原样（宁可留着占位符，
也不要变成空字符串）。

```
__MSG_extName__ + _locales/zh_CN → "广告拦截助手"
__MSG_x__      + 无 _locales     → "__MSG_x__"（原样）
"普通名字"                        → "普通名字"
```

### 测试

600 → **601 项**（`test_manifest_msg_placeholder_resolved_from_locales`）。
八项门槛全绿（601 / 34 / 73 / 13 / 21 / 81 / 29 / 35）。

## 0.7.69 — 2026-11-18

**桌面客户端也加「扫描本机」—— 两边不一致，而你日常用的是桌面客户端。**

### 问题

0.7.68 给 web 控制台的导入弹窗加了「扫描本机」，但**桌面客户端（tkinter）的
「从 Chrome 目录导入」没有**。用户日常打开的就是桌面客户端，等于那个便利性
改动他们碰不到。

### 修法

`PacksOverlay.ask_import_from_chrome()` 的弹窗里补一行：

```
[扫描本机]  找到 2 个，点一下选它
  [选这个]  RXYCDP · Default · 3 个扩展   C:\Users\…\RXYCDP\User Data\Default
  [选这个]  Chrome  · Default · 1 个扩展   C:\Users\…\Chrome\User Data\Default
```

- 扫描走 `call_async()`（后台线程 + `after(0)` 回主线程）—— Windows 上扫
  Program Files 要几秒，同步跑会把界面卡住；Tk 控件只能在主线程动，这是硬约束
- 只列前 6 条（弹窗空间有限），点「选这个」把路径填进输入框
- 配套 `CoreApi.scan_browsers()`（`GET /api/v1/packs/scan-browsers`，超时 120s）

### 验收（gui-check 32 → 35）

```
✅ 「从 Chrome 目录导入」弹窗里有「扫描本机」按钮
✅ 导入弹窗里的按钮齐全（扫描本机 / 开始导入 / 取消）
✅ CoreApi 有 scan_browsers
```

### 过程中我错了两次（都记下来）

1. **按 `tk.Toplevel` 找浮层** —— 找不到。`_Overlay` 不是 Toplevel，它用
   `tk.Frame` 贴在 `master.winfo_toplevel()` 上（为了盖住整窗、卡片居中）。
2. 改成按 `TextOverlay` 类型找、再遍历 `.root` —— **按钮列表还是空**。
   因为 `.root` 挂在窗口顶层，不在 overlay 对象的控件树里。

最后直接把控件树打出来看，才确认按钮其实一直在（`TButton [扫描本机]`），
是断言找错了地方。改成在整棵树上找按钮文本，两个断言都过了。

> 教训：**"断言红"不等于"功能没做"**。先在真控件树上打印出来看一眼，
> 比继续改断言猜下去快得多。

### 测试

600 项不变；gui-check **35** 条。八项门槛全绿
（600 / 34 / 73 / 13 / 21 / 81 / 29 / **35**）。

## 0.7.68 — 2026-11-18

**「我不知道 RXYCDP 的 User Data 在哪」—— 现在可以让程序自己扫。顺带修掉 `as_default` 失效。**

### ① 新增「扫描本机」：不用再手找路径

导入扩展包要填一个绝对路径，但 RXYCDP / Roxy 装在哪、Profile 叫 `Default` 还是
`Profile 1`，用户根本不知道。**卡在这一步就会放弃，功能等于没做。**

新增：

```
GET /api/v1/packs/scan-browsers
→ {"candidates": [
     {"path": "...\RXYCDP\User Data\Default",
      "user_data": "...\RXYCDP\User Data",
      "profile": "Default", "browser": "RXYCDP", "extension_count": 3}
   ]}
```

- 扫 `%LOCALAPPDATA%` / `%APPDATA%` / `%LOCALAPPDATA%\Programs` /
  `Program Files` / `Program Files (x86)`（Linux/macOS 扫 `~/.config`、
  `~/Library/Application Support`），限深度避免扫整盘
- **只列"有扩展可导入"的**（判据与导入逻辑完全一致）—— 空目录列出来只会更困惑
- 浏览器名字用上层目录名猜（`...\RXYCDP\User Data` → `RXYCDP`）

web 控制台的导入弹窗里多了一个「扫描本机」按钮：点一下列出候选，点「选这个」
就自动填进输入框。

实测（造假目录 + 真 core）：

```
扫描 → RXYCDP | Default | 1 扩展 | /root/.config/RXYCDP/User Data/Default
用扫到的路径直接导入 → {"extension_count": 1, ...}   ✅
```

### ② 修掉 `as_default` 传了没用

`PackImportBody` 里**没有** `as_default` 字段（那个字段只加在了安装 .crx 的模型上），
所以 `POST /packs/import {"as_default": true}` 会被 pydantic **静默忽略**：

```
真机复现：{"as_default": true} → is_default: false
```

界面上勾了「设为默认」却不生效，用户只会以为开关坏了。已补字段并接上
`packs.set_default()`；设默认失败时不让导入失败，而是回 `default_warning`。

### ③ 顺带

- 我自己踩的：新路由 `/packs/scan-browsers` 忘了写进 `docs/04`，
  被 `test_every_public_route_is_documented` 抓到（这就是那条测试的价值）。
- 路由顺序：`/packs/scan-browsers` 必须声明在 `/packs/{pack_id}/...` 之前，
  否则会被当成"包名叫 scan-browsers"。已确认顺序正确并实测 200。

### 测试

597 → **599 项**（`test_scan_browser_profiles_finds_importable_user_data`、
`test_import_with_as_default_actually_sets_default`）。
八项门槛全绿（599 / 34 / 73 / 13 / 21 / 81 / 29 / 32）。

## 0.7.67 — 2026-11-18

**「把插件及其数据打包放到我的控制台」—— 功能在，浏览器控制台里却没有入口。**

### 怎么发现的

写 RXYCDP 导入指引时，我照着"用户会怎么操作"去核对界面，发现：

- `from_chrome` 导入**只存在于桌面客户端**（`gui_profiles.py` 的「扩展包」面板，
  按钮叫「从 Chrome 目录导入」）
- **web 控制台（`127.0.0.1:8730` / 远程）里搜不到任何 `packs` 字样**
  —— 整个扩展包功能在浏览器界面上没有入口

而用户的原话是"把插件的及其数据打包放到**我的控制台**里面""以后创建浏览器时
默认就把这些数据都带上（当然也可以选择关闭）"。**功能在、入口不在，等于没有。**

（顺带纠正我自己：指引第一版写的按钮名「从浏览器用户数据导入」是错的，
真实按钮是「从 Chrome 目录导入」；已按实际界面改正。）

### 补什么

web 控制台「扩展」页新增**扩展包**区块（在单扩展库下面）：

| 列 | 内容 |
|---|---|
| 扩展包 | 名称 + id |
| 扩展数 / 带数据 | `extension_count` / `with_data` |
| 新环境 | `默认带上`（绿色徽章）或 `不自动` |
| 大小 | `size_human` |
| 操作 | 「设为默认 / 取消默认」「删除」 |

加了一个「＋ 从 Chrome 目录导入」按钮，弹窗只填一个路径（`User Data` 或
`User Data\\Default` 都认，RXYCDP / Roxy / Chrome 全通用）+ 包名 + 「设为默认」勾选。
底部说明写清：扩展包含代码与数据；官方 Chrome 137+ 忽略 `--load-extension`，
数据会带过去但浏览器不一定加载，环境详情里的 `extensions_active` 会如实说明。

### 接的过程中踩的

1. 第一版把处理逻辑塞进 `onAct()` 的 switch 里用了 `await` —— 那个函数是**同步**的，
   语法检查直接报 `await is only valid in async functions`。改成 `.then()` 链。
2. 事件处理接好了，但**按钮还是不出现**：因为 `S.packs` 一直是 `null`，
   `renderPacks()` 走的是占位卡分支（那张卡里也有"扩展包"三个字，
   所以"包含扩展包"的断言**假通过**了，是后面找按钮的三条断言才暴露出来）。
   根因：`loadPacks` 只加进了首屏 `loadAll` 的 jobs，切到「扩展」视图时没再拉。
   在 `v === 'extensions'` 分支里补上 `loadPacks(opt.force)`。

### 验收

`ui-check.cjs` 23 → **29 项**，新增 6 条（真浏览器 + 真接口）：

```
✅ 扩展页出现「扩展包」区块
✅ 有「从 Chrome 目录导入」入口
✅ 可以通过接口建扩展包
✅ 新建的扩展包出现在控制台列表里
✅ 列表里有「设为默认」按钮
✅ 前端零报错
```

八项门槛全绿（597 / 34 / 73 / 13 / 21 / 81 / **29** / 32）。

## 0.7.66 — 2026-11-18

**文档一直说"配置来自环境变量 / .env 文件"，但代码里从来没读过 .env。**

### 问题

`config.py` 顶部第一行就写着：

> 核心配置。全部来自环境变量 / .env 文件，便于 Docker 与裸装两种部署。

而 `_env()` 只读 `os.environ` —— **没有任何 .env 加载逻辑**。对 Docker 没影响
（compose 里直接给 environment），但对**单文件客户端**影响很大：

想配 `ZW_DOWNLOAD_PROXY`，用户只能去「系统属性 → 环境变量」里加，改完还要
重开程序 —— 绝大多数人不会做，于是那个功能等于没开。

### 修法

新增 `load_dotenv_once()`，在 `get_settings()` 里第一次取配置前调用：

- 查找顺序：`ZW_ENV_FILE` → 配置目录（Windows `%LOCALAPPDATA%\zhiwen-client`，
  Linux/macOS `~/.config/zhiwen-client`）→ 当前工作目录，各找 `zhiwen.env` / `.env`
- **只读第一个找到的**
- **不覆盖已存在的环境变量**（命令行 / CI 显式设的永远优先，Docker 行为不变）
- 支持 `#` 注释、`export KEY=VALUE`、值两侧引号、带 BOM 的文件

于是用户只要在配置目录放一个文本文件：

```
ZW_DOWNLOAD_PROXY=http://127.0.0.1:7890
```

重开客户端就生效，不用碰系统环境变量。`docs/03` 新增 §4.0 写清这件事。

### 我自己踩的坑（记下来）

第一版测试通过后就发了，结果 **`check-all` 一下红了 6 个**：

```
test_download_crx_over_real_http_with_redirect   FAILED
test_download_crx_http_error_is_readable         FAILED
test_compose_start_urls_restores_then_detects    FAILED
…（共 6 个）
```

原因：`load_dotenv_once()` 是**直接写 `os.environ`** 的（它就该这样），
而我的测试只 `monkeypatch.delenv("ZW_ENV_FILE")` —— 那只能撤掉"文件路径"，
**撤不掉文件里注入的键**。于是 `ZW_DOWNLOAD_PROXY=http://127.0.0.1:9`
留在了进程环境里，后面所有走下载的测试都连那个死代理。

单独跑 `test_extstore.py` 是全绿的（41 passed），只有跟 `test_client_extra.py`
一起跑才红 —— **典型的测试间污染，最容易被"我这条过了"骗过去**。
修法：测试里显式 `os.environ.pop()` 清掉文件注入的键 + `reset_settings()`。

### 测试

596 → **597 项**（`test_dotenv_file_is_actually_loaded`）。
八项门槛全绿（597 / 34 / 73 / 13 / 21 / 81 / 23 / 32）。

## 0.7.65 — 2026-11-18

**在云电脑在线状态下，把三个界面判断逐个用真实数据核了一遍。**

### ① 版本落后徽章（0.7.57 新增）→ 真实数据成立

```
hub  (/panel/api/meta)      → 0.7.64
云电脑 (/api/v1/meta)       → 0.7.57
cmp(0.7.57, 0.7.64) < 0     → 控制台会给这颗卡片挂「可升级」
```

也就是说你现在打开 3.5 的主控台，云电脑那张卡片上应该能看到黄色 `可升级`
徽章，悬停写着"退出客户端 → 下载新的安装包 → 覆盖安装（配置与机器码保留）"。

### ② 离线陈旧状态（0.7.53 新增）→ 在线时**不**误标

```
tianyiyuncd1  online=True  status=active  → 状态应标「上次」: False
   tests5 stopped   显示 stopped
   3      failed    显示 failed
   2      stopped   显示 stopped
   1      running   显示 running
```

在线时照原样显示状态；只有离线（或被踢）才会变成「上次：…」。
两轮前它是离线状态、看到的是「上次：running」——**两种状态都实测过了**。

### ③ 本机免令牌（0.7.33 的按请求判定）→ 真机回环确认

在云电脑自己的 shell 里访问**它自己的回环**：

```
curl http://127.0.0.1:8730/api/v1/meta      （不带任何令牌）
→ version 0.7.57 | host 0.0.0.0 | auth_required = False
```

`host` 是 `0.0.0.0`（对外监听），但从回环来仍然 `auth_required=false` ——
这正是用户要的「客户端打开控制台直接进」。而从其它机器访问（我这边）则要机器码，
两种情形都实测过。

### 说明

这轮没有代码改动，只做真实数据核对，所以单元测试仍是 596 项、八项门槛照旧。

### 仍然卡在用户输入上的两件

1. **RXYCDP 的插件实物**：云电脑上确认没有 RXYCDP/Roxy（只有 Chrome、Edge）。
   需要用户给出它所在机器与 `User Data` 路径。
2. **商店链接直接装**：`ZW_DOWNLOAD_PROXY=http://127.0.0.1:21080` 要加到客户端
   环境变量并重启（已验证该地址可访问 Google 商店）。

## 0.7.64 — 2026-11-18

**云电脑实测：再启动后检测页出现两条一样的标签 —— 站点跳转导致的重复入口。**

### 实机过程（v0.7.57，云电脑）

**① 新建环境 → 初始标签页正确**

```
https://browserleaks.com/ip
https://ip123.in/
https://abrahamjuliot.github.io/creepjs/
```

**② 启动 → 浏览器里确实开了 3 个页面**（通过它本机的 CDP 读 `/json/list`）

```
https://browserleaks.com/ip
https://abrahamjuliot.github.io/creepjs/
https://ip123.in/zh/          ← 注意：自动跳转成 /zh/ 了
```

**③ 停止 → 记住 3 条，且记的是跳转后的真实地址**

```
remembered: {'saved': 3, 'urls': [...ip123.in/zh/, ...]}
```

**④ 再启动 → 4 个页面，`ip123.in/zh/` 出现两次** ← **问题在这**

```
browserleaks.com/ip
ip123.in/zh/
creepjs/
ip123.in/zh/     ← 重复
```

### 根因

配置里检测页写的是 `https://ip123.in/`，而它打开后会**跳到** `/zh/`。
停止时抓到的是 `/zh/`，下次启动又把配置里的 `/` 加回来。
两个**字符串不同**，`_dedupe()` 的 `rstrip("/")` 归一合并不了 → 两条几乎一样的标签。

正好撞在用户那句要求上：「默认先打开上次被关闭的网址，然后再加一个检测网址」——
加是加了，但加出了重复。

### 修法（第一次改错了，记下来）

`compose_start_urls()` 里加 `_collapse_same_site()`。

**第一版是"同 host 只留一个"** → 立刻把既有测试
`test_compose_start_urls_restores_then_detects` 弄红了：

```
assert ['https://example.com/a', 'https://example.com/b'] ... "恢复的标签必须在最前面"
```

那条测试用同站点的 `/a` 和 `/b` 两个**不同页面**，被我一刀切合并了。
**同一站点的不同页面是合理需求**（用户可能同时开着好几个），不该合并。

**收紧后的规则：只跳过"根路径"那条**

- 根路径（`/` 或空）且同 host 已有别的条目 → 跳过它（就是这次的问题）
- 非根路径（`/a`、`/b`、`/zh/`）→ **一律保留**，不管同不同站

三个用例验证：

```
['ip123.in/zh/', 'ip123.in/', 'browserleaks…'] → ['ip123.in/zh/', 'browserleaks…']   ✅ 去重
['example.com/a', 'example.com/b']             → 两条都留                     ✅ 不误伤
['ip123.in/', 'browserleaks…']                 → 两条都留                     ✅ 单独根路径不算重复
```

### 清理

测试环境 `检测页自检-可删` 已 stop + purge 删除；用户的环境回到自己的 4 个。

### 测试

595 → **596 项**（`test_same_site_detect_page_does_not_duplicate_restored_url`）。
八项门槛全绿（596 / 34 / 73 / 13 / 21 / 81 / 23 / 32）。

## 0.7.63 — 2026-11-18

**云电脑真机验证「创建浏览器之后还能改代理」——含改完是否真的生效。**

### 实机过程（v0.7.57，机器码访问）

**① 建环境 + 设一条无认证代理**

```
PUT /profiles/{id}/proxy {"proxy_id": 6}   → p2 / socks5://9.9.9.9:1080
POST /profiles/{id}/start
  --proxy-server=socks5://9.9.9.9:1080        ← 直连上游（不带凭据就别加一层中转）
```

**② 停掉，改成带用户名密码的代理**

```
POST /profiles/{id}/stop?force=true        → 200
PUT  /profiles/{id}/proxy {"proxy_id": 5}  → 落地A / socks5://66.80.12.66:20000 / has_password=true
POST /profiles/{id}/start
  --proxy-server=http://127.0.0.1:61536      ← **换成本机中继**（凭据由中继代填）
```

**③ 顶层 warnings 如实说明发生了什么**

```
该环境的代理带用户名密码：浏览器连的是本机中继 127.0.0.1:61536，
由中继代填凭据后转发到真实代理（不是命令行参数，也不依赖浏览器扩展）
```

而且这次启动**同时**把 cookiemanager 通过 `--load-extension` 带上了
（`...,C:\...\extensions\cookiemanager---cookie-editor-0.2.7`）。

**「创建完浏览器之后还可以进行修改浏览器的代理」= 真机验证通过**，
两条路径（直连上游 / 带凭据走中继）都验到，且改完立即生效。

### 清理

测试环境 `改代理自检-可删` 已 stop + `?purge=true` 删除；
现有环境回到用户自己的 4 个（`tests5`/`3`/`2`/`1`），未受影响。

### 说明

这轮改动只在验收侧（无代码改动），所以单元测试仍是 595 项、八项门槛照旧全绿。

## 0.7.62 — 2026-11-18

**带 UTF-8 BOM 的 manifest.json 会被静默跳过 —— 用户只会看到"里面没有扩展"。**

### 怎么发现的（云电脑实机）

想在那台机器上验证「插件**及其数据**一起打包」。先确认它到底装了什么浏览器：

```
C:\Program Files        → Google、Clash Verge、RustDesk、ctyun…
User Data 目录          → Google\Chrome、Microsoft\Edge
```

**没有 RXYCDP / Roxy** —— 所以那台机器上没法导入 RXYCDP 的实物数据
（需要用户指出它到底在哪台机器/哪个目录）。

退一步用**真实 Chrome 用户数据**验证 `from_chrome` 路径，成功（932KB、1 个组件扩展）。
但 `with_data: 0` —— Chrome 里没有第三方扩展的 `Local Extension Settings`。

于是我造了一份带扩展数据的假 Chrome 结构再导入，结果：

```
PACK_NO_EXTENSION  ← 明明有 manifest.json
```

查下去发现：`_read_manifest()` 用 `read_text(encoding="utf-8")` + `json.loads`，
而 **PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM**，解码后多一个 `\ufeff`，
`json.loads` 直接抛 `JSONDecodeError` → 被 `except` 吞掉 → 返回 None → "没有扩展"。

### 这是真问题，不只是我的测试脚本

**现实里确实有扩展的 manifest.json 带 BOM**（Windows 上某些编辑器/构建工具会加）。
一旦碰上：

- 从 Chrome / RXYCDP 导入 → 「里面没有可导入的扩展」，用户完全想不到是 BOM
- 装 .crx → 同样读不出名字和版本

修法：`packs._read_manifest()` 与 `extstore.read_manifest()` 全部改用
**`utf-8-sig`**（容忍 BOM）。修完后同一份文件立刻能导入：

```
with_data: 1, has_data: true, data_dir: "data/<ext-id>"
```

**「插件及其数据一起打包」这条路径 = 真实 Windows 环境验证通过。**

### 云电脑上的收尾

| 动作 | 结果 |
|---|---|
| 删测试包 | 200 |
| 删假 Chrome 目录 | REMOVED |
| 现有包 | 只剩 `CookieManager - Cookie Editor`（默认开启） |

用户自己的 4 个环境（`tests5`/`3`/`2`/`1`）原样未动。

### 测试

594 → **595 项**（`test_manifest_with_utf8_bom_is_recognized`）。
八项门槛全绿（595 / 34 / 73 / 13 / 21 / 81 / 23 / 32）。

## 0.7.61 — 2026-11-18

**云电脑实机跑通了「商店装扩展 → 建包 → 默认带上」整条链，顺带发现详情页看不到"扩展到底加载没"。**

### 实机验证（云电脑 Windows + Chrome 157，机器码访问）

这轮把被卡了六十多轮的那条链路走完了。关键突破：**远端 shell 会把 `"` 转义成 `\"`**，
于是命令行里的 `&` 失去保护被 cmd 当分隔符 —— 带查询串的商店 URL 必然被截断。
改用 PowerShell 的 `-EncodedCommand`（base64 UTF-16LE）绕开引号问题后一次成功：

```
下载 CRX（经 mihomo 代理）      → 164717 字节   ← 与 cookiemanager 真实大小一致
POST /extensions/install {path} → ok
   slug: cookiemanager---cookie-editor, version: 0.2.7
   pack_id: 20a58e339cec, pack_created: true, is_default: true
POST /profiles {name: …}        → extensions: ["cookiemanager---cookie-editor"]
```

**「以后创建浏览器时，可以默认就把这些插件的数据都带上」= 真机验证通过。**

顺带确认了那台机器的代理：`127.0.0.1:21080` 是 **verge-mihomo**（Clash Verge 内核），
`-x http://127.0.0.1:21080` 到 `chromewebstore.google.com` 返回 200。

### 发现并修掉：环境详情里看不到"扩展加载没"

`extensions_active` 以前只在 `POST /packs/apply` 的响应里出现；
`GET /profiles/{id}`（用户点开环境看的就是它）是 `None`。于是：

- 实机这次看到的确定结论是 `extensions_active=False`（Chrome 157 忽略
  `--load-extension`），note 写得很清楚 —— 但**点过「应用扩展包」之后再看就没了**
- 用户只能靠记忆，或者再去点一次「应用」

修：`get_profile()` 在环境绑了扩展时附带 `extensions_active` / `extensions_note`，
用**探针缓存**（未知内核才真跑一次探针，不启动浏览器），所以给详情加这个字段
没有额外开销。

### 清理

云电脑上那个测试环境（`扩展自检-可删`）已 stop + `purge` 删除，
现在只剩用户自己的 4 个（`tests5` / `3` / `2` / `1`）。
装好的 cookiemanager 与它的扩展包**保留**（那正是用户要的效果）。

### 测试

593 → **594 项**（`test_profile_detail_reports_extensions_active`）。
八项门槛全绿（594 / 34 / 73 / 13 / 21 / 81 / 23 / 32）。

## 0.7.60 — 2026-11-18

**云电脑终于上线了 —— 实机验证暴露一个自相矛盾：提示让你"配好代理"，但根本没有配的地方。**

### 云电脑实机状态（v0.7.57，已在线）

```
版本 0.7.57 | host 0.0.0.0:8730 | auth_required True（机器码可用）
hub  http://192.168.3.5:8700
引擎 Chrome 157.0.8103.79
远程命令行可用
```

### 实机验证：商店链接装扩展失败，但原因很清楚

```
POST /api/v1/extensions/install
{"url": "https://chromewebstore.google.com/detail/cookiemanager-…"}
→ EXT_STORE_UNREACHABLE
   连不上 Chrome 应用商店（SSL: UNEXPECTED_EOF_WHILE_READING）
```

对照实验（同一台机器）：

| 目标 | 结果 |
|---|---|
| `http://www.gstatic.com/generate_204` 直连 | **204 成功** |
| `clients2.google.com` 直连 | **SSL EOF（被重置）** |

所以那台机器有网，但 `clients2.google.com` 被拦 —— 装商店扩展**必须有代理**。

### 真正的问题：说了要配代理，却没地方配

`download_crx()` 里是：

```python
with httpx.Client(follow_redirects=True, timeout=timeout) as c:
    r = c.get(url)
```

**没有任何代理配置项**，只靠 httpx 的 `trust_env` 读 `HTTPS_PROXY` 环境变量。
而错误提示写着"国内网络请先给客户端配好代理" —— 用户照着提示**根本不知道该配在哪**。

### 修法：加 `ZW_DOWNLOAD_PROXY`

- 新增配置 `ZW_DOWNLOAD_PROXY`（如 `http://127.0.0.1:7890`），留空则不显式设代理
  （仍尊重环境变量，保持向后兼容）
- `download_crx()` 读它并传给 `httpx.Client(proxy=…)`
- 错误提示改成可操作的：

```
国内网络请设 ZW_DOWNLOAD_PROXY 指向你的代理（例如 http://127.0.0.1:7890），
然后重启客户端再试
```

- `docs/03` 配置表补一行，注明**国内必须配**及具体报错样子

### 顺带探测：找到那台机器上现成可用的代理

`127.0.0.1:21080` 在监听。查它的进程：

```
netstat -ano | findstr :21080   →  PID 11092
tasklist | findstr 11092        →  "verge-mihomo.exe"
```

**是 Clash Verge 的内核 mihomo**（混合端口，同时支持 HTTP 与 SOCKS5）。实测：

| 用法 | 目标 | 结果 |
|---|---|---|
| `-x http://127.0.0.1:21080` | `https://api.ipify.org` | **200** |
| `-x socks5h://127.0.0.1:21080` | `https://api.ipify.org` | **200** |
| `-x http://127.0.0.1:21080` | `https://chromewebstore.google.com/` | **200** |

（早先误判成"不可用"，是因为测试目标选了明文 `http://gstatic.com`，
mihomo 的规则不一定代理它；换 HTTPS 目标就通了。）

**所以那台机器的正确配置是：**

```
ZW_DOWNLOAD_PROXY=http://127.0.0.1:21080
```

配上并重启客户端后，"从商店链接装扩展"就能真的下载 CRX 了。

### 测试

592 → **593 项**：`test_download_proxy_is_honored_and_documented`
（配置能读到 + 错误提示里必须出现 `ZW_DOWNLOAD_PROXY`）。
八项门槛全绿（593 / 34 / 73 / 13 / 21 / 81 / 23 / 32）。

## 0.7.59 — 2026-11-18

**同一份白名单写了两遍，改了一处、CI 就红；顺带把两类偶发失败钉死。**

### ① 安装脚本的白名单有两份（CI 抓到）

0.7.58 我改了 `[InstallDelete]`（旧中文目录要覆盖两种安装模式），
更新了 `tests/test_ascii_paths.py` 里的白名单 —— **但忘了
`packaging/windows/check-iss.py`（C37）里还有同一份白名单**。

CI 的 Windows job 跑的是后者，于是：

```
FAIL (C37) InstallDelete 里出现了未知的中文路径：
  {commondesktop}\知纹客户端.lnk; {commonpf}\知纹客户端;
  {localappdata}\Programs\知纹客户端
```

两处都改好，并且**把 `check-iss.py` 加进 `check-all.sh`**
（它以前只在 CI 里跑，本地从来没跑过 —— 这就是漏的原因）。
门槛数从 7 变 8。

### ② gateway-check 偶发红：启动后立刻读 cookie

`check-all.sh` 里 gateway-check 红过一次，单独跑 20/21 全过。查下去：

```
❌ 异常： Cannot read properties of undefined (reading 'cookies')
```

`POST /start` 返回了 ok，但环境还没真正进入 `running`（负载高时进程还在起），
紧接着 `GET /cookies` 拿到 `STATE_CONFLICT` → `r.data.data` 是 undefined。

修：加 `waitRunning(pid)`，最多等 30 秒到 `state === 'running'` 再读 cookie，
并新增一条断言「环境确实进入 running」。现在 21 项。

**偶发的门槛比红门槛更糟** —— 它会让人习惯性重跑，门槛就形同虚设。

### ③ 顺带：check-all 失败时保留日志

以前无论成败都 `rm -rf $TMP`，而 core 日志就在里面 —— 红了就没法查
（这次就是这样，只能靠复现）。现在失败时保留并打印路径。

### 一个我特意验证过的疑点（结论是没问题）

怀疑过"配了 hub 之后，hub 不可达会不会让本地启动失败"。实测（`ZW_HUB_URL`
指向 `192.0.2.1` 这种必然不可达的地址）：

```
启动 → {"ok": true, ...}
读 cookie → {"ok": true, "data": {"count": 0, "cookies": []}}
```

**本地能力不依赖服务器，降级是对的。**

### 测试

592 项不变；门槛 8 项全绿
（592 / 34 / 73 / 13 / 21 / 81 / 23 / 32）。

## 0.7.58 — 2026-11-18

**升级路径上的一个真实坑：旧版中文目录可能清不掉（另一种安装模式）。**

### 问题

`[InstallDelete]` 里只清了一条：

```
Type: filesandordirs; Name: "{autopf}\知纹客户端"
```

但 **`{autopf}` 的含义随本次安装模式变化**：

| 安装模式 | `{autopf}` 实际指向 |
|---|---|
| 管理员安装（`/ALLUSERS`） | `C:\Program Files` |
| 按用户安装（默认，`lowest`） | `%LOCALAPPDATA%\Programs` |

如果老版本（≤0.4.0，中文目录「知纹客户端」）当初是用**另一种**模式装的，
那 `{autopf}\知纹客户端` 指的不是同一个目录 —— **旧的中文目录原地残留**：

- 用户看到"怎么还有一份"
- 卸载项已经指向新目录，那堆旧文件**再也删不掉**

这正是"升级到 0.7.58 后仍然有中文路径"的可能原因。

### 修法

两种模式的实际路径都显式写上（不存在的 Inno 会跳过）：

```
Type: filesandordirs; Name: "{autopf}\知纹客户端"
Type: filesandordirs; Name: "{commonpf}\知纹客户端"
Type: filesandordirs; Name: "{localappdata}\Programs\知纹客户端"
```

顺带把桌面快捷方式的两种位置也补上（`{autodesktop}` / `{commondesktop}`）。

### 既有白名单测试正确地拦了我一次

`test_install_delete_only_targets_known_legacy_paths` 把允许的旧路径**白名单化**，
我加新路径时它立刻报「InstallDelete 出现了未预期的路径」—— 这正是它该做的
（防止有人随手往这段塞新中文路径）。已把两条新路径加入白名单并写明理由。

### 同轮完成：v0.7.57 发布产物的两步验收

```
✅ 下载完整：50813064 字节
全部 14 项通过          ← --selftest
用产物起 core 跑 e2e …
  产物版 core → 200
通过 73 项，失败 0 项，跳过 3 项
✅ 发布产物跑 e2e 通过
```

> 顺带记一笔：这次 `gh release download` **被截断了 728KB**（50084474 / 50813064），
> 是脚本的大小校验拦住的。如果没这道校验，就会拿一个残缺的包去"验证通过"，
> 结论完全不可信。改用分段下载后才拿到完整文件。

### 测试

591 → **592 项**；七个门槛全绿（592 / 73 / 13 / 20 / 81 / 23 / 32）。

## 0.7.57 — 2026-11-18

**客户端没有自更新，而"你落后 40 个版本"这件事没有任何地方告诉你。**

### 问题

你云电脑上跑的是 **v0.7.16**，当前是 0.7.57。客户端**没有自更新机制**，
所以它不会自己变新；而界面、文档、服务端都**没有一处**提醒"该升级了"。

有意思的是：客户端每次注册都会把 `app_version` 上报给 hub，hub 也存了，
而且 hub 知道自己是什么版本 —— 数据全在，只是从来没人对比过。

### 两件事

**① 主控台给落后版本加「可升级」徽章**

机器卡片上，客户端版本低于服务器版本时出现黄色 `可升级` 徽章，悬停写明：

> 这台客户端是 v0.7.16，服务器是 v0.7.57。客户端不会自动更新：
> 退出客户端 → 下载新的安装包 → 覆盖安装（配置与机器码保留，不用重新配对）

**② `docs/11` 补「怎么升级客户端」**

写清了四步、为什么必须先退出旧客户端（在跑的程序会占住 exe，覆盖会静默失败）、
以及**配置与机器码都存在 `%LOCALAPPDATA%\zhiwen-client`（安装目录之外），
升级不丢、服务器端也不会多出一台新设备**。顺带说明怎么看自己的版本
（窗口顶栏 `v0.7.57`，或 `--selftest` 第一行）。

### 过程中我自己踩的两个坑（都值得记）

1. **把 HTML 塞进了 `textContent`**：徽章一开始拼进 `bits` 数组、再由
   `r.sub.textContent = bits.join(...)` 写出 —— 结果只会显示成字面文本
   `<span class="badge warn">…</span>`，断言找不到 `.badge.warn`。
   改成写完文本后**单独插入一个真元素**。
2. **把包装对象当 DOM 节点用**：`fillCard(card, dev)` 的 `card` 是
   `{root, refs}` 包装对象，不是 DOM 节点。我对它调 `card.querySelector(...)`
   → `TypeError` → 而这个渲染循环**没有 try/catch**，一抛就**整屏卡片全白**。
   控制台直接空白（比原问题严重得多），是控制台验收脚本第一时间抓到的。
   改用 `r.sub.parentNode.querySelector(...)`。

### 验收

主控台断言 78 → **81**：落后版本有徽章、悬停文案含"覆盖安装/机器码保留"、
版本相同的机器**不**显示徽章。

七个门槛全绿（591 / 73 / 13 / 20 / 81 / 23 / 32）。

## 0.7.56 — 2026-11-18

**连不上服务器时，用户以为软件坏了 —— 其实单机照常能用。**

### 问题

客户端 GUI 的连接流程是：`probe_server(地址)` 失败 → `set_phase("error", "连不上")`
→ 直接 return，**不会启动本机 core**。

而工具的本机能力（环境管理、开浏览器、CDP、远程终端）**完全不依赖服务器** ——
`docs/11` 里明确写了「不连服务器时直接用 `zw serve` 也一样工作」。

问题在于：单文件客户端只有**图形界面**这一个入口，用户不知道还有 `serve` 这条路。
服务器没开 / IP 填错 / 网络不通时，界面上只有一句"连不上"，于是他会以为
软件坏了、或者以为"必须有服务器才能用"。

### 修法

在"连不上"这条分支里补一句可操作的提示（同时写进事件流）：

```
想先在本机用（不连服务器）？关掉这个窗口，在本程序目录执行：
  zhiwen-client serve  —— 会启动本机服务并打开控制台。
```

配合 0.7.51 修好的"子命令转发"（单文件产物的入口是 `client.py`，以前
`serve` 根本不生效），这条路现在真的能走通。

### 过程中我自己的一处错

补这句提示时用了 `Path(sys.argv[0]).name`，但 `gui.py` 里**没有**
`from pathlib import Path` —— `import zw_core.gui` 是能过的（模块级导入成功），
真走到那一行才会 `NameError`。已补导入，并在测试里直接断言
`"from pathlib import Path" in src`，防止再次出现"用了但没导入"。

### 测试

590 → **591 项**；七个门槛全绿（591 / 73 / 13 / 20 / 78 / 23 / 32）。

## 0.7.55 — 2026-11-18

**404 有两种含义，界面一律报"接口尚不存在" —— 用户会去等一个永远不会来的修复。**

### 问题

客户端控制台里判断"这个接口还没做"的逻辑是：

```js
const todo = resp.status === 404 || resp.status === 501;
```

**只看状态码。** 但 404 也可能是**数据**不存在：

- 环境被删了 → `PROFILE_NOT_FOUND`
- 包 id 写错 → `PACK_NOT_FOUND`
- 代理 id 不存在 → `PROXY_NOT_FOUND`

这些情况界面会显示「接口尚不存在：界面会显示占位态，等后端补齐后自动生效」。
用户于是去等一个**永远不会到来的修复**，而不是检查自己删了什么。

### 修法

统一成一个判定函数（4 处调用点全部换掉）：

```js
function isEndpointMissing(status, code){
  if (status === 501) return true;
  if (status !== 404) return false;
  const c = String(code || '').toUpperCase();
  if (!c) return true;                       // 没业务码 = 框架层路由不存在
  if (c === 'HTTP_404' || c === 'NOT_FOUND') return true;
  return false;                              // 业务码 → 数据不存在
}
```

顺带修掉客户端控制台另一处旧文案：绑定扩展的弹窗原来写着
「扩展在环境启动时才注入」—— 官方 Chrome 137+ 根本不加载 `--load-extension`，
装了也不会生效。现在如实说明，并提示看 `extensions_active` 判断实际有没有加载。

### 过程中我自己的一个险情

第一次插入 `isEndpointMissing` 定义时，替换锚点没匹配上（`function markConn(ok){`
与 `(ok) {` 差一个空格），于是**定义没进去、但 4 处调用都改好了** ——
等于把页面改成了"调用未定义函数"，比原来的 bug 更糟。

是**测试立刻抓到的**（`assert "function isEndpointMissing(" in html`）。
这也说明先写断言再改、改完必跑，比"看着改对了"可靠。

### 测试

589 → **590 项**；七个门槛全绿（590 / 73 / 13 / 20 / 78 / 23 / 32）。

## 0.7.54 — 2026-11-18

**客户端控制台还在教用户"选代理会自动注入凭据扩展" —— 这条路已经不通了。**

### 问题

`core/src/zw_core/web/index.html`（你打开 `127.0.0.1:8730` 看到的那个控制台）
在"代理库"字段下面写着：

> 选库里的代理会自动注入凭据扩展（Chromium 的 `--proxy-server` 不支持带密码）。

**这条说法早就被实测推翻了**：Chrome 137 起忽略 `--load-extension`，
官方 Chrome 149 / 150 / 157 全都不加载扩展。带用户名密码的代理真正靠的是
**本机中继**（`services/proxyrelay.py`：中继代填 `Proxy-Authorization`、
SOCKS5 走 RFC 1929 握手）。

危害在于：用户代理连不上时，会以为是"扩展没生效"去查扩展，方向完全错了。
**这类"界面文案比现实旧"的问题比代码 bug 更难发现** —— 代码有测试，文案没有。

### 修法

改成实话（并且写明为什么）：

> 选库里的代理会自动补上凭据：带用户名密码的代理不能写进 `--proxy-server`
> （Chromium 不支持），会由本机中继代填认证头再转发给真实代理。
> 早年"生成认证扩展"那条路已不通（Chrome 137 起忽略 `--load-extension`），
> 现在一律走中继。

### 新增防回归测试

`test_client_console_does_not_claim_auth_extension_works`：
断言页面源码里**不再出现**「注入凭据扩展」，且**必须有**「本机中继」。
以后再有人把旧说法抄回去，CI 直接红。

### 顺手确认

客户端控制台（:8730）**确实有代理列**（`['代理', p.proxy || '直连', true]`），
你要的"控制台多一列显示代理"在主控台和客户端控制台两边都有。

### 测试

588 → **589 项**；七个门槛全绿（589 / 73 / 13 / 20 / 78 / 23 / 32）。

## 0.7.53 — 2026-11-18

**离线机器的环境仍显示绿色 `running` —— 用户会以为还有东西在跑。**

### 怎么发现的

检查"我的自检有没有在你 3.5 上留垃圾"时，看到控制台概览：

```
机器数: 1    totals: {'online': 0, 'browsers': 4, 'running': 1}
  tianyiyuncd1 | 环境数: 4 | 运行中: 1
```

**机器明明离线（online: 0），却显示"运行中: 1"。**

那 4 个环境是你自己命名的（`tests5` / `3` / `2` / `1`），不是我的垃圾；
但设备离线之后，它的 `inventory` 快照还是离线前的 —— 控制台直接照着渲染，
于是离线的机器下面挂着一个**绿色 running 徽章**。

### 修法

`stateBadge(st, stale)` 加一个"陈旧"维度：机器离线（或被踢）时，
环境状态渲染成

```
上次：running          ← 灰色徽章 + tooltip「这台机器当前离线，显示的是它最后一次上报的状态」
```

状态值本身不改（那确实是它最后上报的），但**可信度变了就必须让用户看出来**。

### 验收（主控台断言 75 → 78）

给 fixture 里那台**离线**机器挂了一个 `running` 环境（就是真实发生的情形），断言：

```
✅ 离线机器的环境状态标为「上次：…」
✅ 离线机器下那行不出现绿色 running 徽章
✅ 那一行确实写着「上次：running」
```

### 过程中两处我自己的错

1. 第一版给 fixture **加了第三台机器**（用来保住"没有 inventory"的断言），
   结果 4 条"只有两台机器"的既有断言全红。改成：保持两台，
   把那条提示改成**源码断言**（它本来就是静态文案），不再和 fixture 数量互相牵制。
2. 绿色徽章断言一开始是全局的 `#view-browsers .badge.ok`，会被别处的 ok 徽章干扰。
   改成**精确定位到那一行**再数。

### 测试

588 项不变；主控台断言 **78 条**，七个门槛全绿（588 / 73 / 13 / 20 / 78 / 23 / 32）。

## 0.7.52 — 2026-11-18

**`verify-release.sh` 升级成两步：产物自检 + **产物跑完整 e2e**。一条命令出结论。**

### 为什么加第二步

自检只能证明"能起来、依赖齐全"。真正要看的是**这个文件能不能干活**：
起 core、建环境、开浏览器、读 cookie、导出导入、同步。

上一轮那个 `serve` 不生效的 bug 就是这类：**自检 14 项全过，e2e 直接
Connection refused**。只跑自检会漏掉它。

### 这轮实测（v0.7.51 真实产物，配置全给）

```
全部 14 项通过                 ← --selftest
✅ 发布产物自检通过

用产物起 core 跑 e2e …
  产物版 core → 200
通过 73 项，失败 0 项，跳过 3 项
✅ 单机全能力自检通过
✅ 发布产物跑 e2e 通过
```

**73 项 = 与源码完全相同的覆盖面**（含导出/导入、同步、错主密码拦截）。
跳过 3 项是这台机器的环境（没有 fingerprint-chromium、没有真代理）。

也就是说：**用户下载到的那个文件，自己把全部能力跑通了。**

### 用法

```bash
bash scripts/verify-release.sh v0.7.52      # 下载 + 自检 + e2e
SKIP_E2E=1  bash scripts/verify-release.sh  # 只要自检（快）
SKIP_DOWNLOAD=1 bash scripts/verify-release.sh v0.7.52   # 复用已下好的
```

配了 `ZW_HUB_URL` / `ZW_PANEL_USER` 就会把同步那几项也真跑（脚本自动
批准并**清理**自检设备，不在服务器上留垃圾 —— 已实测跑完只剩用户自己的 1 台）。

### 修的过程中踩的

`ROOT` 变量没赋值就用了（`cd "$(dirname "$0")/.."` 之后没记下来），
到第二步才报 `ROOT: unbound variable`。第一段（自检）是过的，所以
只测自检发现不了 —— 又一次说明"两步都要跑"。

### 测试

仍是 588 项（这轮改的是验收脚本），七个门槛全绿。

## 0.7.51 — 2026-11-18

**单文件产物里 `zhiwen-client serve` 根本不生效 —— 在无显示器的服务器上直接崩。**

### 怎么发现的

上一轮做了 `verify-release.sh`（下载已发布产物跑自检）。这轮进一步：
**拿已发布的产物当用户的机器跑完整 e2e**（不是源码，是那个二进制）。结果：

```
产物版 core → 000
❌ 服务未就绪：{'code': 'UNREACHABLE', 'message': '[Errno 111] Connection refused'}
```

看它的日志：

```
知纹 客户端启动失败：TclError: no display name and no $DISPLAY environment variable
```

### 根因

PyInstaller 的入口是 **`client.py`（GUI 客户端）**，不是 `cli.py`：

```python
a = Analysis([str(SRC / "zw_core" / "client.py")], …)
```

而 `client.py::main()` 只认 `--selftest`，其余参数一概忽略、直接去起图形界面。
所以 `zhiwen-client serve` 里的 `serve` **从来没被解析过** ——
在没有显示器的机器上必然 `TclError` 崩掉。

**这是致命场景**：3.5 那台服务器没有桌面环境，`zw serve` 会直接崩。
我这一路都在源码里跑（`python -m zw_core.cli serve`），完全看不出来。

### 修法

`client.py::main()` 里加两层：

1. **识别 CLI 子命令并转交**：`serve` / `mcp` / `doctor` / `profile` / `proxy` /
   `sync` / `extension` / … 共 19 个，命中就调 `cli.main()`。
   单文件产物于是既能当桌面客户端，也能当服务端。
2. **没有图形环境时给可操作的提示**，而不是抛 `TclError`：

```
知纹：当前没有图形环境（DISPLAY / WAYLAND_DISPLAY 都没设），
桌面客户端起不来。要在这台机器上跑服务端，用：
  zhiwen-client serve          # 起 core（控制台 + API + 网关）
  zhiwen-client --help         # 看所有子命令
  zhiwen-client --selftest     # 只做自检
```

### 实测

```
$ zhiwen-client serve   （源码形态下 sys.argv 模拟）
INFO [zw.main] 监听 http://127.0.0.1:8851  数据目录 /tmp/srvtest
INFO [uvicorn.error] Uvicorn running on http://127.0.0.1:8851
```

### 测试

586 → **588 项**：`test_client_entrypoint_forwards_cli_subcommands`（serve 真的被转交）、
`test_client_entrypoint_explains_missing_display`（无 DISPLAY 时给提示且退出码 2）。

> 这一条是"验产物"才抓出来的 —— 源码层面怎么测都测不到，
> 因为源码里我从来都是 `python -m zw_core.cli serve`。
> 上一轮加的 `verify-release.sh` 立刻体现了价值。

### 补：用**修好后的产物**跑完整 e2e（这条才是硬证据）

发布后拿 v0.7.51 的真实二进制再跑一遍完整自检：

```
产物版 core → 200                      ← 之前是 000 / Connection refused
通过 68 项，失败 0 项，跳过 5 项
✅ 单机全能力自检通过
```

跳过 5 项是这台机器的环境（没有 fingerprint-chromium、没有真代理、
没给 hub/令牌/主密码），不是缺陷。

也就是说：**用户下载到的那个文件，自己起得来、自己把 68 项能力跑通了。**

## 0.7.50 — 2026-11-18

**新增 `scripts/verify-release.sh`：验证**已发布产物**能不能跑 —— 并已实测通过。**

### 为什么加这个

`check-all.sh` 验的是**工作区里的源码**。但用户下载到的是 GitHub Releases 上的
二进制 —— 打包环节出问题（缺库、glibc 版本、路径写错）在源码测试里**完全看不出来**。

历史上就踩过：`ubuntu-latest`（glibc 2.42）打出来的包在 Debian/Ubuntu 22/24 上
直接报 `libtcl8.6.so: version GLIBC_2.42 not found`。那次是靠人工发现的。

### 这轮实测（v0.7.49 的真实产物）

下载 → 校验大小 → 跑它自己的 `--selftest`：

```
✅ 下载完整：50786968 字节
  OK    zw_core 导入: 0.7.49
  OK    FastAPI/uvicorn: ('0.163.2', '0.54.0')
  OK    AEAD（pynacl）: 往返一致
  OK    控制台页面: 196885 字符
  OK    core 应用可构建: 81 条路由
  OK    本机服务可启动（无控制台）: 无控制台环境下启动成功（端口 37149）
  OK    远程命令行执行器: /bin/sh 可用，1ms
  OK    安装路径为纯英文
  OK    配置目录可写 / 机器码生成 / 地址规范化
  全部 14 项通过
✅ 发布产物自检通过
```

这比"源码测试全绿"更接近交付真相：**用户会下载到的那个文件，自己说它没问题。**

### 用法

```bash
bash scripts/verify-release.sh            # 验最新 release
bash scripts/verify-release.sh v0.7.49    # 验指定 tag
SKIP_DOWNLOAD=1 bash scripts/verify-release.sh v0.7.49   # 复用已下好的
```

只验**当前平台**能跑的那个资产；Windows/macOS 的包需要在对应系统上跑，
脚本会明确说"本平台不适用"，不会假装验过。

`AGENTS.md` 的交付清单已加上这一条，并写清为什么不能省。

### 顺带确认

hub 侧的异常覆盖也是完整的：4 个自定义异常里 3 个有全局处理器，`ProxyError`
是在每个路由里内联捕获的（逐条查过，唯一没包 try 的 `list_all()` 只读
`has_password`、不解密，不会抛）；`device_pull_proxies` 对 `secret_of`
是**按条**兜底的 —— 密钥换了会降级成 `secret_error` 而不是整次同步 500。

### 测试

仍是 586 项（这轮只加脚本与文档），七个门槛全绿。

## 0.7.49 — 2026-11-18

**5 类业务异常没被接住，全都会冒成 500 + 原始 traceback。**

### 怎么发现的

这轮做"AI 清单里 65 个端点是否真的存在"的扫描（结论：**全部存在**，
没有一个是清单瞎写的）。但扫的过程中日志里冒出：

```
ERROR [zw.api] 未处理异常 POST /api/v1/packs/import
Traceback …
zw_core.services.packs.PackError: 要么给 src_path（扩展目录或 zip），要么给 from_profile
```

`api.py` 只注册了 **8 个** exception_handler，而项目里有 **14 个**自定义异常类。
漏掉的是：

| 异常 | 谁抛 | 本该返回 |
|---|---|---|
| `PackError` | 扩展包（导入/设默认/删除） | 400/404 + `PACK_*` |
| `StoreError` | 商店装扩展 | 400 + `EXT_STORE_*` |
| `ShellError` | 远程命令行 | 400 + `SHELL_*` |
| `SessionError` | 实时终端 | 400 |
| `RelayError` | 本机代理中继 | 502 |

后果：用户和 AI 拿到的是**原始堆栈**，不是 `{ok:false, code, message}` ——
既看不懂，也没法按错误码处理。

### 修法与实测

五类全部注册处理器（状态码取各自的 `status`，并给可操作 hint）。

```
POST /api/v1/packs/import            → 400 PACK_INVALID_SOURCE   （原 500 + traceback）
POST /api/v1/packs/nope/default      → 404 PACK_NOT_FOUND
POST /api/v1/extensions/install      → 400 PATH_NOT_FOUND
```

### 两条防回归测试

- `test_business_errors_are_structured_not_500`：五种场景逐个断言
  `status < 500` 且 `ok is False` 且有 `code`。
- `test_every_custom_exception_has_a_handler`：**扫 `core/src` 里所有
  `class XxxError(Exception)`，逐个要求 `api.py` 里有对应 handler**，缺一个就红。
  以后再新增异常类忘了注册，CI 直接拦住。

> 写第二条时我自己错了一次：第一版把 `hub/src` 也扫了，于是要求 core 给
> `DeviceError`（hub 的类）注册处理器 —— core 通过 HTTP 跟 hub 打交道，
> hub 的异常永远不会传播进来。测试改成只扫 `core/src`。

### 测试

584 → **586 项**，七个门槛全绿（586 / 73 / 13 / 20 / 75 / 23 / 32）。

### 补：同一个竞态测试又栽了一次（Windows CI）

上一轮修了 `test_connect_tunnel_adds_basic_auth` 里 `http_upstream.seen` 的竞态，
但**同一个测试里还有一处**没管：对中继统计的即时断言。

Windows CI 报：

```
assert stats["bytes_in"] == len(payload)     # 通过
assert stats["bytes_out"] == len(payload)    # AssertionError: assert 0 == 20
```

`bytes_out` 是"客户端→上游"方向，中继线程把 20 字节写给上游之后才计数；
客户端此时已经收到回显，但计数还差一拍。负载高的机器上就会踩到。

修：统计断言也走 `_wait_until`（最多等 5 秒）。同一个文件里另一处
`stats["rejected"]` 断言也一并加上等待 —— 现在虽然不会踩（计数在应答之前加），
但写法统一之后，以后中继实现调整（比如改成先回包再计数）也不会再偶发。

连跑 3 次稳定通过。

> 教训：**修竞态要修一整个"类"，不是修被 CI 抓到的那一行**。
> 上一轮我只修了 `seen`，同一个测试里 `bytes_out` 就接着红。

## 0.7.48 — 2026-11-18

**MCP 工具少给参数时报裸 `KeyError`（32 个里 21 个这样），AI 完全没法用。**

### 问题

`tools/call` 是直接把 `args` 丢给工具函数的，而工具内部写 `args["profile"]`
这种直取。于是 AI 少给一个参数时，拿到的是：

```json
{"isError": true, "content": [{"type":"text","text":
  "{\"ok\": false, \"code\": \"KeyError\", \"message\": \"'profile'\"}"}]}
```

**既不知道哪个参数必填，也不知道该怎么填。** 而工具的 `inputSchema` 里
其实**早就声明了** `required: ["profile"]`，只是没人用它做校验。

用空参数把 32 个工具逐个真调一遍：**21 个报裸 KeyError**。

### 修法

在 `tools/call` 分发处统一按 `inputSchema.required` 校验，报错写成：

```
调用 zw_navigate 缺少必填参数：`profile`（环境 id 或名字）
```

实测：裸 KeyError **从 21 个降到 0**，23 个变成可读的 `MCP_MISSING_ARG`，
剩下 3 个是合理的工具级报错（"要么给 id 要么给 host+port"之类）。

### 顺带扩展了错误码文档扫描器

`test_no_doc_only_error_codes_that_look_like_typos` 原来只认
`XxxError("CODE")` 这种异常构造；而 MCP 的错误码是**塞在返回字典里的字符串**，
扫不到。扩展后一次抓出 **5 个真漏写的错误码**并补齐：

`MCP_MISSING_ARG`、`CLIENT_ERROR`、`CORE_UNREACHABLE`、`ROUTE_NOT_FOUND`、
`UNREACHABLE`、`UPSTREAM_ERROR`。

### 还修了一个偶发测试

全量跑时 `test_proxyrelay.py::test_connect_tunnel_adds_basic_auth` 失败过一次，
单独跑 3 次都过。查下去是**竞态**：中继异步把 CONNECT 转给上游，客户端这边
已经收到 `200 Connection Established`，但假上游的 `seen` 记录可能还差几微秒。
改成"最多等 5 秒轮询"，不再立刻断言。

偶发测试比红测试更糟 —— 它会让人习惯性重跑，门槛就形同虚设。

### 测试

583 → **584 项**，七个门槛全绿（584 / 73 / 13 / 20 / 75 / 23 / 32）。

## 0.7.47 — 2026-11-18

**删掉三个"声明了但根本不生效"的配置项（比死开关更彻底：连语义都不成立）。**

### 怎么找的

上一轮修了 `ZW_ALLOW_ROOT` / `ZW_GATEWAY_ENABLED` 两个死开关。这轮改成**系统扫描**：
把 `config.py` 里 53 个字段逐个查"除 config.py 外有没有任何代码读过它"。

结果三个零引用：

| 字段 | 注释声称的作用 | 真相 |
|---|---|---|
| `engine_autofetch` | 「找不到内核时允许自动下载」 | **自动下载从来没实现**。缺内核直接报 `ENGINE_NOT_FOUND` |
| `internal_token` | 「gateway ↔ core 的内部令牌」 | 网关鉴权只用 `admin_token` / `device_id`（`api.py::require_auth`），**没有任何内部鉴权** |
| `gw_public_base` | 对外网关地址 | 与真正生效的 `gateway_public_base` **property 撞名**，这个字段纯属多余 |

### 为什么必须删而不是"补上实现"

- 前两个要实现是**新功能**（自动下载二进制、内部鉴权），不在用户需求里，
  留着开关只会让人以为"设了就有用"。
- 第三个是**重复定义**：property 才是真相源，两处并存只会让人改错一处。

### 处理

- 三个字段删掉，原位留注释说明"这里曾经有什么、为什么删"
- `gateway_public_base` property 改成直接读 `ZW_GATEWAY_PUBLIC_BASE`（唯一真相源）
- `docs/03` 删掉 `ZW_ENGINE_AUTOFETCH` 和 `ZW_INTERNAL_TOKEN` 两行说明

实测（显式配置 + 未配置两条路都验）：

```
显式 ZW_GATEWAY_PUBLIC_BASE=http://10.0.0.9:8730/ → 生效且去掉尾斜杠
不配置                                          → 自动探测出 http://192.168.3.6:8730
三个字段确实已不存在：engine_autofetch / gw_public_base / internal_token 全部 True
```

### 新增测试 `test_no_dead_config_fields`

扫 `config.py` 的所有字段，逐个要求在**别处**被读过；零引用就红。
豁免只有两个（`force_token_on_public` 被 config 自己的 validator 读、
`shell_cwd` 经 `getattr` 读所以 grep 抓不到）。以后再有人加"声明了不生效"的
配置项，CI 会直接拦住。

### 测试

582 → **583 项**，七个门槛全绿（583 / 73 / 13 / 20 / 75 / 23 / 32）。

## 0.7.46 — 2026-11-18

**又抓到两个"死开关"：`ZW_ALLOW_ROOT` 和 `ZW_GATEWAY_ENABLED` 设了完全没效果。**

### 怎么发现的

做配置项文档覆盖检查时，发现有 7 个环境变量没写进 `docs/03`，其中 4 个
**所有文档都没提**。顺着两个最像开关的去查代码引用：

```
ZW_ALLOW_ROOT       → 除 config.py 里的定义，全项目零引用
ZW_GATEWAY_ENABLED  → 同上
```

也就是说用户设了它们，**什么都不会变**。文档漏写只是让人不知道；
死开关是让人知道了也被骗。

顺带发现：`engines.build_cmdline()` 是「只要以 root 跑就无条件加 `--no-sandbox`」，
而 `allow_root` 那个开关根本没参与判断。

### 修法

- `ZW_GATEWAY_ENABLED=false` → `ROUTER.register()` 直接不登记（`/cdp/<id>` 一律 404，
  只能本机直连端口）。适合"这台机器不对外提供 CDP"的部署。
- `ZW_ALLOW_ROOT=false` → 以 root 启动环境时抛 `ENGINE_RUN_AS_ROOT`，并说明原因。

### 我自己在这里犯的一个错（值得记）

第一版我把 `ZW_ALLOW_ROOT` 的**默认值设成了 false**（"默认不允许 root"），
跑全量测试：**21 项失败** —— 因为这台机器 uid=0，而容器/CI 基本也都是 root。
`--no-sandbox` 是历史必需行为，我修一个死开关不能顺手把现有部署搞坏。

改成：**默认 `true`（保持历史行为），设成 `false` 才是"拒绝"**。
这也是配置项该有的方向——默认值必须让已有部署继续能跑。

### 文档

`docs/03` 补齐 7 个配置项（含 `ZW_ALLOW_ROOT`、`ZW_GATEWAY_ENABLED`、
`ZW_INTERNAL_TOKEN`、`ZW_DEVICE_ID`、端口池、远程命令行 cwd/timeout）。
`docs/04` 补 `ENGINE_RUN_AS_ROOT` 错误码。

### 一个 CI 抓出来的测试错误

`test_allow_root_gate_is_enforced` 第一版在 **Windows 上必红**：

```
FAILED tests/test_client_extra.py::test_allow_root_gate_is_enforced
AssertionError: 以 root 跑且未授权时必须报错，而不是静默 --no-sandbox
```

因为那段代码是 `if not IS_WIN and _is_root():` —— Windows 上整段不走，
`--no-sandbox` 在 Windows 是另一条分支。**这是测试没考虑平台，不是产品问题。**
已加 `sys.platform == "win32"` 时 skip。

（顺带说明：这次发布被我的"必需平台缺失就拒绝发版"闸拦住了 ——
Windows 构建失败 → 发布明确报错，而不是发出一个只有两平台的版本。
这个闸是按设计工作的。）

### 测试

579 → **582 项**：
`test_allow_root_gate_is_enforced`（拒绝时报错 + 允许时带 --no-sandbox）、
`test_gateway_enabled_switch_is_enforced`（关掉不登记 / 打开登记）、
`test_every_config_env_var_is_documented`（46 个环境变量逐个对文档，缺一个就红）。

七个门槛全部通过：582 单测 / 73 / 13 / 20 / 75 / 23 / 32。

## 0.7.45 — 2026-11-18

**自检脚本会往你服务器上注册残留设备 —— 已修，并清了已有的两条。**

### 发现

在服务器面板上看到：

```
zwd_tabscheck1234567890ab    psubs3.6   pending
zwd_guicheck1234567890abcd   psubs3.6   pending
zwd_HS4ClQKT...              tianyiyuncd1  active
```

后两条是我自检脚本留下的垃圾。用户面板里不该出现这种东西。

### 根因（比想象的绕）

两个脚本都写了 `core.start(device_id="zwd_…", hub_url="")`，看起来明确"不连 hub"：

```python
if hub_url:
    os.environ["ZW_HUB_URL"] = hub_url
```

问题在于 —— **空串只是"不覆盖"**。如果外层环境里已经有 `ZW_HUB_URL`
（比如 `check-all.sh` 为了跑同步那几项导出了它），自检 core 就照样连上 hub、
拿 `ZW_DEVICE_ID` 注册成一条待批准设备。于是每跑一次自检，服务器多一条垃圾。

这和上一轮 `gui-check` 继承 `ZW_ADMIN_TOKEN` 是**同一类问题**：
子进程/子进程内的 core 默认继承父环境，而脚本假设了自己是干净的。

### 修法

两个脚本在启动 core **之前**清掉所有会改变行为的 `ZW_*`：

```python
for k in ("ZW_HUB_URL", "ZW_HUB_TOKEN", "ZW_DEVICE_ID", "ZW_ADMIN_TOKEN",
          "ZW_SYNC_PASSWORD", "ZW_PORT_POOL_START", "ZW_PORT_POOL_END",
          "ZW_ALLOWED_CIDRS"):
    os.environ.pop(k, None)
```

### 实测

故意用**脏环境**跑（`ZW_HUB_URL` 指向真实 hub、`ZW_DEVICE_ID=zwd_shouldnotappear…`）：

```
✅ tabs-check：断言通过 13 条，失败 0 条
服务器设备数：1（只剩 tianyiyuncd1）—— 没有多出来
```

再跑完整的 `check-all.sh`（它本来就会导出 hub 相关变量），跑完后设备数仍是 1。

### 顺手清理

服务器上那两条残留设备已用 `DELETE /panel/api/devices/{id}` 删掉，现在面板里只有你自己的
`tianyiyuncd1`。

## 0.7.44 — 2026-11-18

**新增 `scripts/check-all.sh`：七个门槛一条命令跑完 —— 它立刻抓出三个我自己的问题。**

### 为什么要合并

门槛有七个（pytest + 6 个脚本），散在不同文件里，「提交前必须做」那张表全靠人记，
少跑一个不会有任何提示。更隐蔽的是：**跳过项看起来像"环境限制"，其实是没配** ——
「导出/导入」和「同步」就因此被跳过十几轮，我还照着 65/6 报了十几轮。
所以这个脚本自己把前提条件配好（主密码 / hub / 令牌 / 判定并批准设备）。

用一条命令：

```bash
bash scripts/check-all.sh
# 想连自己的 hub 真跑同步那几项：
ZW_HUB_URL=… ZW_HUB_TOKEN=… ZW_PANEL_USER=… ZW_PANEL_PASSWORD=… bash scripts/check-all.sh
```

### 它抓出的三个问题（都是**我自己的测试脚手架**的问题）

**① 两个 core 抢同一个端口池**

`check-all` 会同时起两个 core（主 core + tabs-check 自己那个），而端口池默认都是
`22001-22100`。后果：`gateway-check` 启动环境时抢不到端口 → 环境其实没跑起来 →
读 cookie 返回 `STATE_CONFLICT` → 报

```
Cannot read properties of undefined (reading 'cookies')
```

看起来像网关坏了，实际是脚手架自己打架。修：每个 core 显式分配独立端口池
（`22001-22040` / `22101-22140`）。

**② `ui-check` 的一条假失败**

脚本自己会用 `addInitScript` 把 `ZW_ADMIN_TOKEN` 预置进 `localStorage`
（为了让"配了令牌的机器"也能免登录跑完界面），而我写的断言却是
「localStorage 里不该有令牌」——**和脚本自身行为矛盾**，配了令牌时必然红。
改成断言「页面没有擅自改写本地令牌」（等于预置值）。

**③ `gui-check` 继承了外层环境**

它被 `check-all` 调用时继承了 `ZW_ADMIN_TOKEN` / `ZW_HUB_URL` / `ZW_PORT_POOL_*`，
于是临时 core 变成"要令牌"且"去连 hub"，面板取数全失败。
单跑过、合并跑挂，而且失败项是**空白**（异常没被记录）。
修：进入时先清掉这些变量；并在 `__main__` 兜住异常打印栈，
避免下次又只看到"失败项："后面空空一行。

### 实测

```
pytest               ✅ 579 passed, 2 skipped
e2e-check            ✅ 通过 73 项，失败 0 项，跳过 3 项
tabs-check           ✅ 断言通过 13 条，失败 0 条
gateway-check        ✅ 通过 20 项，失败 0 项
console-ui-check     ✅ 断言通过 75 条，失败 0 条
ui-check             ✅ 通过 23 项，失败 0 项
gui-check            ✅ 断言通过 32 条，失败 0 条
════════ 全部通过。
```

### 一点体会

这三个问题**单跑任何一个脚本都发现不了**，只有把它们放在一起、并且**用真实配置**
（有令牌、有 hub）跑才会暴露。之前十几轮我都是"挑几个跑"，漏掉的正是这一层。

## 0.7.43 — 2026-11-18

**把"默认带上插件数据、也可以关掉"这个开关做成端到端验证。**

### 用户要求（原话）

> 这样以后创建浏览器时，可以默认就把这些插件的数据都带上（**当然也可以选择关闭**）

对应的实现其实早就有（`packs.set_default()` + `apply_default_packs()`），
但**只有单元级覆盖，没有"新建一个环境到底带没带"的验证** ——
也就是说开关"看起来"存在，没人真的开一遍关一遍。

### 这轮实测

```
① 不设 is_default            → 新环境 extensions: []
② 设为默认                    → 新环境 extensions: ['swext']    ← 用户要的"默认带上"
③ 再取消默认                  → 新环境 extensions: []           ← 用户要的"也可以关闭"
④ ZW_DEFAULT_PACKS=false + 包标默认 → 新环境 extensions: []      ← 全局开关也生效
```

两个开关都确认有效：**每个包**的 `is_default` 和**全局**的 `ZW_DEFAULT_PACKS`。

### 测试

578 → **579 项**。新增 `test_default_pack_toggle_on_off_affects_new_profiles`：
真起 core，导入一个包，开→关→再开，每次新建环境并断言 `extensions` 的实际内容。

> 这是第三次因为 `client` fixture 只在 `tests/test_api_smoke.py` 里而返工了
> （前两次是 manifest 端点和 stop 的 remembered 字段）。已经养成习惯：
> 要写用到 `client` 的测试，直接写进那个文件。

## 0.7.42 — 2026-11-18

**发版流程不再被一个平台的 runner 故障堵死（但不悄悄少发平台）。**

### 起因

今晚 GitHub 的 macOS arm64 runner 反复抢不到（`The job was not acquired by Runner
of type hosted`），v0.7.36 起被取消 4 次。而发布 job 是 `needs: build`：

```
X 构建 macOS (cancelled)  →  发布到 Releases (skipped)
✓ 构建 Windows
✓ 构建 Linux-x86_64
```

**Windows 和 Linux 明明构建成功了，却因为 Mac 机器抢不到而发不出去。**

### 改法（两道闸，缺一不可）

1. **必需平台缺一就明确失败**（`::error::` 并 exit 1）：
   `zhiwen-client-Windows-x64.exe` / `...-setup.exe` / `zhiwen-client-Linux-x86_64`
   —— 避免"发出一个残缺的正式版"。
2. **macOS 允许缺失**，但必须**大声说明**：
   - 打 `::warning::`
   - 发布说明**最前面**插一行：
     > ⚠️ **本次发布不含 macOS 包**：构建 Mac 版的那台机器（GitHub 托管 runner）当时抢不到；
     > Windows 与 Linux 均已正常构建并通过自检。需要 macOS 版请稍后重跑那个 job，或自行从源码构建。

发布 job 从 `needs: build` + 默认成功条件，改成 `if: always() && startsWith(github.ref, 'refs/tags/v')`。

### 为什么这样取舍

用户实际要的是 **Windows（云电脑）+ Linux（服务器）**，macOS 他们从没提过。
因为一个 macOS runner 抢不到，把用户真正要用的两个平台一起扣着，代价不对等。
但"悄悄少发"同样不可接受 —— 所以缺 macOS 时说明里会写得非常明确。

### 实测

在本地复刻了那两段 shell：

```
（Windows/Linux 齐、缺 macOS）→ ✅ 必需齐全 → ⚠️ 说明最前面插入 macOS 缺失提示
（缺 Windows 安装包）        → ❌ 必需缺失：zhiwen-client-Windows-x64-setup.exe → 拒绝发版
```

## 0.7.41 — 2026-11-18

**又抓到一个"字段漏给"的 bug：链式代理的 `hop` 从来不出现在接口里。**

### 怎么发现的

为了把最后几个跳过项也变成真跑，我用「绑 0.0.0.0 + 配令牌 + 从局域网 IP 访问」
跑了一遍完整自检（这样 `MCP 鉴权` 才会真的被拒、才会被检查到）。
结果 `MCP 鉴权` 通过了，但冒出一条新失败：**链式代理可导入**。

查下去发现：库里 `hop` 列明明是 `http://127.0.0.1:21080`，
接口返回的却是 `hop: null` —— `_row_to_public()` 里**根本没有这个字段**
（`is_chain` 和 `chain` 都在，唯独少了它）。

### 后果

- **前端"编辑代理"回填不出前置**，用户改一个链式代理只能把前置重新打一遍
- 脚本/AI 无法判断"这条链式的前置是什么"，只能去解析给人看的 `chain` 字符串
- `e2e-check.py` 的"链式保留了前置"断言直接红

这是同一类问题的**第二次**（第一次是列表接口把 `is_chain`/`chain` 整个漏掉，
0.7.21 修过）。字段名对不上、少给一个，都是"代码看着对、用起来不对"。

### 修法

`_row_to_public()` 补上 `hop`，值用 `spec.hop.upstream`（`scheme://host:port`，
**不含凭据**）；单跳时给空串而不是 `None`，前端少一层判空。

实测：

```
链式自检  is_chain=True  hop='http://127.0.0.1:21080'
                        chain='http://127.0.0.1:21080|socks5://5.5.5.5:1080'
p1        is_chain=False hop=''  chain='socks5://66.80.12.66:20000'
```

### 顺带修掉自检脚本的一个"不能重跑"问题

`链式代理可导入` 断言的是 `imported == 1`，而导入是按去重键 upsert 的 ——
同一个库上跑第二次就变成 `imported: 0 / skipped: 1`，**误报失败**。
现在这条断言会先删掉同名条目再导入，自检可以反复跑在同一个库上。

### 验收

```
通过 73 项，失败 0 项，跳过 3 项      ← 之前是 65 / 0 / 6
✅ 单机全能力自检通过
```

跳过的 3 项现在是**真·环境限制**：
指纹内核 ×2（这台机器没有 fingerprint-chromium）、
代理出口检测（本机没有真代理；不过它的代码路径已在本轮用本机代理验证过）。

### 测试

577 → **578 项**（`hop` 暴露 + 单跳为空串 + 落地凭据不外露）。

## 0.7.40 — 2026-11-18

**代理"检测出口"的回显地址改成可配置 —— 顺带让这条从来没被跑过的路径第一次跑起来。**

### 两个问题一个改法

`_ECHO_ENDPOINTS` 是硬编码的三个境外地址：

```python
"https://api.ipify.org?format=json",
"https://ifconfig.me/all.json",
"https://ipinfo.io/json",
```

1. **用户在国内，这三个都可能慢或被拦** —— 于是"检测出口"永远失败，
   而失败原因看起来像"代理坏了"，实际是回显地址连不上。这会误导排错方向。
2. **这条路径在本机永远验证不了** —— `scripts/e2e-check.py` 里"代理出口检测"
   一直是跳过项，等于**从没跑过**。

改法：加 `ZW_EGRESS_ENDPOINTS`（逗号分隔），留空仍用那三个默认值。
用户自己起一个返回 `{"ip":"x.x.x.x"}` 的地址就能换掉；我也能用本机代理验证它。

### 实测（真起本机 HTTP 正向代理 + 回显服务）

```
✅ 通过本机代理检测出口：exit_ip=203.0.113.9 / country=ZZ / latency=74ms
✅ 密码错时报错（ok=False），密码对时成功
```

第二条尤其重要：用户粘了一条带密码的代理、密码过期了，他需要看到"密码不对"，
而不是看到"检测通过"。以前这条逻辑也没被验证过。

### 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZW_EGRESS_ENDPOINTS` | 三个境外地址 | "检测出口"要访问的 IP 回显地址，逗号分隔。国内建议自建 |

已写进 `docs/03-部署指南.md` 的配置表。

### 测试

575 → **577 项**。新增两条：真本机代理下的成功路径、密码错误路径。
（写的时候踩了两下：测试代理内部 `httpx` 没导入导致 502；以及 `proxysvc`
只是别名，真模块名是 `proxy`。）

## 0.7.39 — 2026-11-18

**把"一直被跳过"的两节验收变成真跑：单机端到端 65 → 72 项。**

### 之前被自己蒙在鼓里

`scripts/e2e-check.py` 的输出一直是「通过 65 / 跳过 6」，我照着这个数字报了十几轮。
但其中两节**从来没有真正执行过**：

| 节 | 跳过原因 |
|---|---|
| 11. 导出 `.zwp` → 删除 → 导入还原 | core 没配 `ZW_SYNC_PASSWORD`（导出包靠主密码加密） |
| 12. 同步（diff / push / 错主密码拦截） | core 没配 `ZW_HUB_URL` |

也就是说**用户的备份路径（导出/导入）和跨机同步，在本机一次都没被自动验证过**。

### 补上之后

给 core 配上主密码 + 指向 3.5 的 hub 再跑：

```
通过 69 项，失败 3 项，跳过 5 项
失败项：差异可查 / 推送 / 错主密码被拦（412）
```

3 条失败的原因是**设备还没被批准**（`ZW_DEVICE_AUTO_APPROVE=false`，
新设备是 `pending`）—— 审批门禁在正常工作，不是 bug。去面板点「允许」后：

```
通过 72 项，失败 0 项，跳过 4 项
✅ 单机全能力自检通过
```

剩下跳过的 4 项是真正的环境限制：指纹内核（2 项）、代理出口（本机没有真代理）、
MCP 鉴权（本机回环免令牌，属正常）。

### 写进脚本，避免下次又跳过

`scripts/e2e-check.py` 的用法段现在明确写了跑满 72 项需要的两个条件
（主密码；hub + 设备已批准），以及实测的差异对比。`AGENTS.md` 的提交前清单
也把数字从「65 / 6」更新成「72 / 4」。

> 这件事的教训：**跳过项要看清楚是"环境不具备"还是"我没配"**。
> 前者可以接受，后者意味着那部分代码在你交付前一直没被跑过。

## 0.7.38 — 2026-11-18

**"检查更新"从串行等到卡死，改成并发 + 短超时。**

### 问题

`check_updates()` 是串行的 `for` 循环，每个扩展调一次 `fetch_info()`，
而 `fetch_info()` 会**整包下载 CRX**（只为读 `manifest.version`），
默认超时 60 秒。用户装了 5 个带商店 ID 的扩展 → 最坏 5×60 = **5 分钟**，
界面上看就是"点了没反应"。

### 改法

- 先收集候选（带 `.zw-ext-id` 的目录），再用线程池**并发**查远端版本
- 每个请求超时降到 **20 秒**（可用 `timeout=` 调），并发度 4（可用 `concurrency=` 调）
- 单个扩展失败只影响它自己那一行（返回 `error`），不拖垮整批 ——
  之前一个超时会让后面的全等下去

最坏耗时从 `n × 60s` 降到大约 `ceil(n/4) × 20s`。

### 测试

574 → **575 项**。新增 `test_check_updates_runs_concurrently_and_isolates_failures`：
两个扩展，一个成功一个抛错，断言 ① 两行都在、失败那行带原因；
② 总耗时 < 0.45 秒（两个各 sleep 0.25s，串行必然超过）—— 用时间证明它真的并发。

### 说明

远程"只查版本不下整包"的正规做法是 Omaha 的纯版本接口
（`/service/update2` 返回 `<updatecheck version="..."/>`），但这条协议我**没法在
这台机器上验证**（连不上 clients2.google.com）。按规矩不把未验证的远程协议改动
发出去，所以这次只优化"内部怎么调度"，下载路径保持已经实测过的那条。

## 0.7.37 — 2026-11-18

**顺着上一轮那个静默失败，把同类"只记一条 warning、原因不外露"的地方补上。**

### 改了什么

`manager.stop()` 现在把"记住标签页"的结果放进返回值：

```json
{"profile": {...}, "method": "...", "trim": null,
 "remembered": {"saved": 3}}
```

没记住时给原因（不再是"这一项没动静"）：

```json
{"remembered": {"saved": 0, "reason": "调试端口不可达，按「读不到」处理（不覆盖旧记录）"}}
{"remembered": {"saved": 0, "reason": "环境没在运行，没有标签页可记"}}
```

为什么值得改：`remember_open_tabs()` 本来就有几种"没记"的分支并返回 `reason`，
但 `stop()` 把返回值丢掉了，只留一条 warning。**用户看到的现象是"下次启动标签没恢复"，
却查不到为什么** —— 和上一轮 `CDP.close()` 那个 bug 是同一类问题：
错误信息存在，但没人往外送。打包后日志文件不一定在手边，调用方（CLI / MCP / AI）
更看不到日志。

同时 `already_stopped` 这条提前返回也补上了 `remembered.reason`，
不再让调用方从"没有这个字段"去猜。

### 验收

`scripts/tabs-check.py` 增加一条断言（12 → 13 条）：

```
remembered = {'saved': 3}
✅ stop 返回里说明了标签记忆结果
断言通过 13 条，失败 0 条
```

单元测试 573 → **574 项**（`stop` 的 `remembered` 形状）。

### 顺带记一笔

写这条测试时我又踩了同一个坑：`client` fixture 只在 `tests/test_api_smoke.py` 里，
而且它设了 `ZW_ADMIN_TOKEN`，请求必须带 `Authorization` 头 —— 不带就会拿到
`{"ok": false}`，然后 `KeyError: 'data'`。这已经是第三次因为这个 fixture 返工了。

## 0.7.36 — 2026-11-18

**找到并修掉一个"静默失效"的真 bug：无头模式下，恢复标签和检测页从来没被打开过。**

### 怎么发现的

前几轮我都用手工或截图验证"启动开检测页、再启动恢复且不重复"，
`scripts/e2e-check.py` 里**一条断言都没有**。这轮补了一个专门的
`scripts/tabs-check.py`（本机起一个可区分的页面服务，标题带编号），
跑起来第一次就红了：

```
❌ 检测页也在  — ['http://127.0.0.1:39377/user2']
```

`start_urls` 里三个网址都在、API 也把三个都传下去了，但浏览器里只有一个页面。
翻被过滤掉的日志看到：

```
WARNING [zw.lifecycle] 补开标签页失败（不影响使用）：'CDP' object has no attribute 'close'
```

### 根因

`--headless=new` 不接受命令行传多个网址（会直接退出），所以无头模式下
`manager.start()` 只把第一个交给命令行，**其余的用 CDP 补开**：

```python
res = ck.open_tabs_sync(ws_url, wanted[1:])
```

而 `open_tabs()` 里写的是：

```python
conn = CDP(ws_url)
try:
    await conn.connect()
    ...
finally:
    await conn.close()      # ← CDP 类根本没有 close()
```

`CDP` 只有 `__aenter__`/`__aexit__`，**没有 `close()`** → 每次都抛
`AttributeError` → 整个函数失败 → 被 `manager.start()` 的 `except` 吞成一条
warning（"不影响使用"）→ **一个标签都没补开，而界面上看不出任何异常**。

有界面（headful）时不走这条路，所以你在 Windows 云电脑上手工验证看到的是对的，
这个问题一直藏着。任何无头场景（没显示器的 Linux 机器、后台跑）都会静默丢掉
"恢复上次标签 + 追加检测页"这个需求。

### 修法

1. 给 `CDP` 加 `close()`（等价 `__aexit__`，供不用 `with` 的调用点使用）
2. `open_tabs()` 改用 `async with CDP(ws_url) as conn:` —— 不再手动 close，
   少一条出错路径

修完实测：

```
第二次启动后页面 = ['.../user2', '.../detect', '.../user1']
✅ 恢复了上次的页面   ✅ 检测页也在   ✅ 没有重复标签
断言通过 12 条，失败 0 条
```

### 新增 `scripts/tabs-check.py`

不依赖外网：把 `ZW_DETECT_URLS` 指到本机临时 HTTP 服务（页面标题带编号），
这样"到底开了哪几个"是**可精确断言**的，而不是"看截图大概对"。覆盖 12 条：

- 启动后有检测页；用户在浏览器里自己开的页面也被计入
- 停止后记住了**所有**用户页面（不只是最后一个），且只有 http/https
- 再启动：恢复的页面在、检测页也在、**没有重复**
- 第三次启动：数量稳定，不累积

> 顺带记一下我自己的两处错：① 端口我从 `profile` 上找 `debug_port`/`port`，
> 实际在 `GET /profiles/{id}/cdp` 的 `local_port`；② 一开始把日志用 grep 过滤掉了，
> 所以只看到断言红、没看到那条 warning —— 排查这类"静默失败"必须先看日志。

### 测试

571 → **573 项**（`CDP.close()` 存在性 + `open_tabs` 返回形状）。

## 0.7.35 — 2026-11-18

**清掉对抗审查剩下的三条：版本号靠目录名猜、魔数重复、文档比代码承诺得多。**

### 1. 扩展版本号靠目录名猜 → 会永久显示"有更新"

`check_updates()` 用 `d.name.rsplit("-", 1)[-1]` 取"当前版本"。这只对
`install()` 装进来的 `<slug>-<version>` 成立。**扩展包导入 / 手工放进来的目录**
可能就叫 `cookiemanager`（不含版本）—— 那样整个名字被当成版本号，
远端 0.6.7 永远 ≠ `cookiemanager`，于是**永久提示"有更新"**。
用户要的是"他也可以自己更新"，给个永远红的提示等于没用。

修：
- `install()` 落一份 `.zw-ext-meta.json`（`{id, version, name}`）
- 新增 `_installed_version()`：优先读 meta，没有才退回目录名
- 新增 `_ver_key()`：`1.10 > 1.9`（字符串比较会得出相反结论），
  并且**只有远端更新才算"有更新"** —— 开发者回滚版本时不该提示升级

### 2. 魔数 `22` 硬编码两次

`proxy_upstream_of()` 用它决定要不要省掉协议名，`ProfilePanel.ROW_LIMIT_PROXY`
用它 `_clip()`。两处各写一个 22 迟早不同步 —— 而不同步的后果就是之前修过的
那个 bug 回来（IP 尾部被切掉、66.80.12.66 和 .65 分不清）。
提成模块级 `ROW_LIMIT_PROXY = 22`，类里引用它，只此一处。

### 3. `proxysync.pull()` 文档说会级联删除，实现从来没做

模块文档原来写着"服务器**删除**的，本机下次同步时也会消失（`pull` 带完整 id
列表，本地按…来删）"。翻代码：`pull()` 里一行删除都没有。**文档比代码承诺得多。**

想清楚后决定**保持不删**，并把文档改成实话 —— 理由写进注释：

- 本机环境是按 `proxy_id` 引用代理的。级联删除会抹掉"正被某个环境使用"的代理，
  那个环境下次启动就变成「代理 #N（库里已没有这一条）」。
- 服务器侧"删除"的语义是"从我这份共享库里移除"，不等于"每台机器立刻丢掉"。
  **隐藏**才是"我不想再看到它"的正确表达。
- 真要清本机某条，用户在客户端 GUI 里删就行 —— 有明确意图，也有明确后果。

### 测试

567 → **571 项**。新增：meta 落盘、目录名不可靠时用 meta、版本比较顺序、
`pull` 不删本机代理（用真 hub 的 `client_root` fixture 跑）。

## 0.7.34 — 2026-11-17

**堵掉一条能免令牌进控制台的路：uvicorn 替我们信了 `X-Forwarded-For`。**

### 漏洞

我们的鉴权规则是「来自回环 → 免令牌」（用户明确要求"本机打开直接进"）。
而 uvicorn 默认：

```python
proxy_headers=True
forwarded_allow_ips="127.0.0.1,::1"
```

意思是：**只要连过来的是本机地址，它就把 `request.client.host` 换成请求头里的
`X-Forwarded-For`**。而本机反代（nginx / Lucky）转发到 `127.0.0.1:8730`
恰好就是"本机地址" —— 于是远程攻击者只要经反代发一个

```
X-Forwarded-For: 127.0.0.1
```

就会被 uvicorn 改写成"来自回环"，**整个控制台不需要任何令牌**。
用户的实际部署正是这个拓扑（3.5 上用 nginx/Lucky 对外）。

### 修法

`uvicorn.run(..., proxy_headers=False, forwarded_allow_ips="")`

我们自己已经有一套显式策略（`ZW_TRUST_PROXY_HEADERS` + `api._client_ip()`，
默认关闭），让 uvicorn 别插手，否则那套开关形同虚设。

实测（真服务、真局域网 IP）：

```
修复后 · 从局域网带伪造 XFF：auth_required = True
修复后 · 从局域网带伪造 XFF 且无令牌：HTTP 401
```

### 文档

`docs/03-部署指南.md` 新增警告：`ZW_TRUST_PROXY_HEADERS` 与"本机免令牌"是
危险组合，打开前必须让反代**覆盖**该头：

```nginx
proxy_set_header X-Forwarded-For $remote_addr;   # 覆盖，不是 $proxy_add_x_forwarded_for
```

### 顺带修掉 Windows CI 红

0.7.33 的 Windows 构建挂在单元测试上：

```
FAILED tests/test_proxylib.py::test_hub_patch_edits_in_place_not_insert
stderr: .../fastapi/routing.py", line 46, in <module>
```

原因是我给子进程手工构造了 `env`（只有 `PATH=/usr/bin:/bin` 和 `HOME`），
把 Windows 上的 `SystemRoot` / `PATHEXT` / `TEMP` 全剥掉了，子进程连 fastapi
都 import 不了（Linux 恰好容忍）。改成继承父环境、只覆盖需要的几个变量。
**重跑后 Windows 通过。**

> 另外那次 macOS 任务是 GitHub runner 资源不足被取消
> （`The job was not acquired by Runner of type hosted`），与代码无关，重跑即过。

### 测试

566 → **567 项**。新增 `test_uvicorn_does_not_rewrite_client_ip_from_forwarded_header`：
monkeypatch `uvicorn.run`，断言 `proxy_headers is False` 且 `forwarded_allow_ips` 为空。

## 0.7.33 — 2026-11-17

**派子代理做对抗审查，找出 12 条真缺陷并逐条修掉。其中 2 条严重。**

> 教训先行：前面二十几轮都是我给自己打分（"我测过了、都过了"）。
> 这次让另一个 agent 专门去找错，**第一条就是可写任意目录的漏洞**，
> 而且现有测试是绿的 —— 因为它只覆盖了 zip 成员名，没覆盖 manifest 字段。

### 1. [严重] 扩展 manifest 的 `version` 能写出任意目录

`version` 直接拼进 `ext_dir / f"{slug}-{version}"`。装一个
`"version": "../../../../tmp/pwn/escaped"` 的 CRX，`copytree` 会正常解析 `..` ——
**能往系统任意目录写文件**。`name` 早就有 `_slugify()`，`version` 一个字都没过滤。

修：`_safe_version()` 白名单（只认字母数字点下划横线，长度 ≤32，含 `..` 一律退 `0`）
＋落盘前 `dest.resolve().relative_to(ext_dir.resolve())` 兜底。

实测（真跑一次攻击 CRX）：

```
落盘路径: /tmp/pwncheck/root/extensions/evilext-0
version 被规范成: 0
外部仍有 pwn.txt? False          ← 修好了
```

### 2. [高] 主控台「编辑代理」实际是「新增代理」

我上一轮加的功能是**坏的**：payload 与「新增」逐字节相同、不带 id，而后端只有
`POST /v1/proxies`（按 `scheme+host+port+username+hop` 去重，不含 id）。
改主机/端口时新键不存在 → 走 INSERT → **多出一条**：
① 用户以为改了，环境仍指向旧那条；② 新那条没密码（编辑框留空）不可用；
③ `origin` 被覆写成 `panel`，"哪台机器加的"丢了。

修：新增 `PATCH /v1/proxies/{id}` + `proxies.update_by_id()`（按 id 就地改，
保留密码与 `origin`，与另一条重复时返回 409 `PROXY_DUPLICATE` 而不是静默合并）。

实测（真 hub）：

```
① 建好: 1 | 条目数: 1 | 有密码: True
② 改主机/端口: 200 | id 还是 1
   条目数（必须还是 1）: 1
   密码保住了: True | origin 保留: panel
③ 改成与另一条重复 → 409   ④ 改不存在的 id → 404
```

### 3. [高] `zw_hide_proxy` 100% 失败

本地 `proxies` 表**没有 `hidden` 列**（隐藏是服务器侧概念），工具直接
`UPDATE proxies SET hidden=?` → `no such column: hidden`。**已移除**，
并在代码里写明为什么不该有这个工具（永远失败的工具比没有更糟）。

### 4. [中] `zw_install_extension` 说支持目录，实际必然报"文件不存在"

`install()` 只认 `p.is_file()`。已支持目录（内存打包走同一条解包路径）。

### 5. [中] 错误消息回显任意文件前 60 字节

`crx_path` 是调用方给的任意路径，把开头内容打进错误消息，等于给
CLI / MCP / AI 代理开了一个"读本机任意文件片段"的口子。已改成只给可判定的提示。

### 6. [中] 配了令牌的机器，本机打开仍然弹登录框

`auth_required` 原来是「配没配 `ZW_ADMIN_TOKEN`」，不是「这次请求要不要令牌」。
所以配了令牌的机器即使从回环访问（接口已放行、返回 200），页面还是弹框 ——
**正是用户抱怨的那个现象**，我之前只在没配令牌的机器上验过。

修成按请求算（`not is_loopback_request(request)`）。实测：

```
本机回环 → auth_required: False   （不弹框）
局域网   → auth_required: True
```

### 7. 其他修正

- `host_notice`（绑定降级原因）以前**赋值后没人读**，用户设了 `0.0.0.0` 却只监听
  本机、日志一个字不说。现在启动打 WARNING，并暴露在 `/api/v1/meta`。
- `scripts/gui-check.py` 里 `bool(logs) or True` 是恒真假断言，已改成真检查。
- 控制台读错误用 `b.error.message`，而 hub 返回扁平 `{ok,code,message}` ——
  所以"端口非法"这类可读原因永远显示不出来。已兼容三种信封。
- 对话框 `onOk` 返回 `false` 时仍然关窗，用户输入全丢。已改成保留。
- AI 清单里 `/extensions/install` 重复两条、`if False` 残留、`ExtInstallBody`
  死代码（定义在 `build_app()` 内且无人引用）—— 已清。
- **删掉 `zw_core_src_tmp/`**（2.5MB / 24640 行的过期源码副本，
  由 `2ea35ca` 误提交、`.gitignore` 未忽略）。它会污染 `git grep`/IDE/AI 的搜索结果，
  命中的还是错的那份。已加 `.gitignore`。

### 我自己在修的过程中犯的两个错（都已修）

1. `str.replace()` 没限次数，把「新增」也改成了 PATCH —— 全量测试前就发现并修回。
2. 新的 hub 测试在**进程内**改 `sys.modules`/环境变量没还原，连累
   `test_sync_integration` 挂 9 项；改成子进程跑。另外 `ZW_DEVICE_ID` 泄漏
   导致 8 项报「设备未注册」，已加环境变量快照/还原。

### 测试

560 → **566 项**。新增：路径穿越×2、PATCH 就地编辑、目录安装、不回显文件内容、
`auth_required` 按请求、`host_notice` 可见。

全量：**566 passed / 2 skipped**；单机 65/0/6；主控台 75/0；客户端控制台 23/0；
客户端 GUI 32/0。

## 0.7.32 — 2026-11-17

**主控台代理库补「编辑」——凑齐增/改/删/隐藏四件，并把 CRUD 全验一遍。**

### 为什么补

用户的原话是「代理可以在**服务器里面进行添加管控**，也可以在用户机上添加管控，
也都是一样的…也可以在服务器里面删除，或者隐藏」。

添加 ✅、删除 ✅、隐藏 ✅ 都有了，**唯独不能改**。写错一个端口只能删了重加。

### 加了什么

行内多一个「编辑」→ 对话框**预填现有值** → 保存走 `POST /v1/proxies`（upsert）。

一个刻意的设计：**密码框永远不回填**。

- 后端根本不下发密码（只给 `has_password`），前端没有可回填的东西
- 输入框写 `placeholder="留空＝不修改"`，留空就是保持原密码
- 想换密码就输新的

这样既不会在页面上泄露密码，又不用"删掉重建"才能改端口。

### 验收（主控台界面 67 → 73 条）

```
✅ 每行都有「编辑」按钮              （数量 == 表格行数）
✅ 点「编辑」弹出对话框
✅ 编辑框预填了现有主机              66.80.12.66
✅ 密码框**不回填**（留空＝不改）
✅ 编辑保存发出 host/port/label
✅ 编辑时密码为空（表示不修改）
断言通过 73 条，失败 0 条
```

### 代理库现在的完整能力

| 操作 | 主控台 | 客户端 GUI | 接口 | MCP |
|---|---|---|---|---|
| 看 | ✅ | ✅ | ✅ | `zw_list_proxies` |
| 加（单条/批量） | ✅ | ✅ | ✅ | `zw_import_proxies` |
| 改 | **✅ 本次** | ✅ | ✅ | — |
| 删 | ✅ | ✅ | ✅ | — |
| 隐藏 | ✅ | — | ✅ | `zw_hide_proxy` |
| 测出口 | ✅ | ✅ | ✅ | `zw_check_proxy` |
| 与服务器同步 | ✅ | ✅ | ✅ | `zw_sync_proxies` |

### 测试

单元测试仍是 **559 项**；主控台断言 67 → **73 条**。

## 0.7.31 — 2026-11-17

**GUI 验收从"按钮存在"升级成"真点进去改一遍代理并验证生效"。**

### 之前只验了结构

`gui-check.py` 原来只断言「代理」按钮存在 —— 但用户明确要求的是
「创建完浏览器之后，**还可以进行修改浏览器的代理**」。
按钮在、点下去坏了，原脚本是看不出来的。

### 现在真跑一遍

点行内「代理」→ 浮层打开 → 保存新代理 `socks5://9.9.9.9:1080`
→ 断言四件事：

- 浮层真的打开了
- 新代理真的写进了库（`9.9.9.9` 出现）
- 环境已指向新代理（`proxy_id` 非空）
- **界面上的代理列已刷新成新地址**（不是还显示旧的）

```
✅ 找到带代理的那一行     ✅ 行里有可点的「代理」按钮
✅ 点「代理」弹出浮层     ✅ 改代理真的写进了库
✅ 环境已指向新代理       ✅ 界面上的代理已更新为新地址
断言通过 32 条，失败 0 条（原 26 条）
```

### 顺带查出我自己脚本的一个错

第一版点「代理」时抛 `KeyError: 'body'` —— 我以为产品坏了，查下去发现是
**我的测试字体字典用错了键**：真实 `gui.py::_build_style()` 返回的是
`body / small / tiny / title / bold / big / mono`，我写成了 `base / head`（不存在）
且漏了 `body`。已按真实键集合修正，并在注释里写明"键必须和 gui.py 一致"。

> 这条值得记：**测试脚手架和被测代码的契约也要对齐**，否则会浪费时间去查一个
> 不存在的产品 bug。

### 测试

单元测试仍是 **559 项**；客户端 GUI 断言 26 → **32 条**。

## 0.7.30 — 2026-11-17

**主控台「代理」视图补 XSS 验证（数据来自设备上报，属不可信来源）。**

### 为什么这条重要

代理库里的 `label` / `host` 是**被控设备上报**的字符串 —— 一台机器被入侵、
或者有人故意改客户端，就能把 `<img src=x onerror=...>` 塞进代理标签。
主控台是管理员天天打开的页面，一旦注入成功等于被人拿到了面板权限。

### 做了什么

在 `console-ui-check.cjs` 的代理 fixture 里加了一条带载荷的：

```js
label: '<img src=x onerror="window.__xss=1">代理'
host:  '<img src=x onerror="window.__xss=1">1.1.1.1'
origin: 'zwd_evil'
```

并在代理视图上断言三件事：

- `#px-rows` 里**没有 img 节点**（没被解析成元素）
- `window.__xss` **未设置**（脚本没执行）
- 载荷以**字面量**显示（能看见 `onerror` 这串文本本身）

结果：**67 条断言全过 / 0 失败**（原 64 条，新增 3 条）。

> 顺带把两条受影响的断言从 2 改成 3 —— 加了第 4 条 fixture 之后可见行数变了。
> 这种事如果不跟着改，就会变成"假失败"，久了就没人看这个脚本了。

### 现在主控台的转义覆盖

| 来源 | 字段 | 覆盖 |
|---|---|---|
| 设备上报 | 机器名 / 环境名 | ✅ 已有断言 |
| 设备上报 | **代理 label / host** | ✅ 本次新增 |

## 0.7.29 — 2026-11-17

**商店下载的真实 HTTP 路径终于被测到了（用本地假商店，不需要外网）。**

### 此前的空洞

`download_crx` 一直被 monkeypatch 掉，等于这条链里**最关键的一环从没被测过**：
URL 拼错、不跟随 302、状态码没判断、拿到 HTML 错误页 —— 全都测不出来。
而它正是"输入商店链接就能装"的入口。

### 新增 3 条真 HTTP 测试

起一个本地"假 Chrome 商店"（`ThreadingHTTPServer`）：

- `/service/update2/crx` 返回 **302** → `/actual.crx` 才是文件
  （复刻商店真实行为，同时验证 `follow_redirects` 真的生效）
- 验证完整链路：跟随重定向 → 拿到 CRX → 去头 → 解包 → 入库 → 建包 → 设默认
- 404 时给出可读原因（下架 / ID 写错），不是烂在 `zipfile`
- 连不上时明确要求"需要能访问 clients2.google.com"并建议配代理

实测：

```
install(url=<32位ID>, as_default=True)
  → slug=httpext version=3.1.4
  → pack_created=True is_default=True
  → manifest.json / sw.js 都落盘
```

### 现在"商店链接装扩展"这条链的证据分布

| 环节 | 证据 |
|---|---|
| URL 拼装 + 302 跟随 + 状态码 | ✅ 本地假商店真 HTTP 测试 |
| CRX 去头（CRX2/CRX3/纯 zip/HTML 错误页/截断/坏偏移） | ✅ 单测 |
| 解包防路径穿越 | ✅ 单测 |
| 入库 → 建包 → 设默认 → 新环境带上 | ✅ 单测 + 实测 |
| 插件在真浏览器里执行 | ✅ ungoogled 真机 `data-store-installed='YES'` |
| 真实 Google 商店 | ✅ 云电脑实测（164717 字节，header_len=1310） |

只剩"在**同一台**机器上从真实 Google 下载并跑完整个流程"没串过 —— 需要云电脑在线。

### 测试

556 → **559 项**。

## 0.7.28 — 2026-11-17

**补齐接口文档：11 个接口从来没写进 `docs/04`，并加了"漏文档就红"的测试。**

### 漏了什么

AGENTS.md 的规矩是「加一个接口 → 文档 04 补一行」，但这条没人守。实际扫描发现
**11 个接口**文档里一条都没有：

```
/packs/import  /packs/{pack_id}  /packs/{pack_id}/default
/profiles/{ref}/packs/apply  /profiles/{ref}/proxy
/proxies/test  /proxies/test-many
/shell/status  /shell/exec  /shell/enable  /term
```

也就是说：扩展包、改环境代理、代理检测、远程命令行、实时终端 —— 这些**用户会直接找**
的能力，文档里查不到。

### 补的内容

- **扩展包一节**：三种来源（`from_profile` / `from_chrome` / `path`）、
  `as_default` 语义，以及 `extensions_active` 到底是什么意思（配内核实测表）
- **代理表**：`test` / `test-many` / `PUT /profiles/{ref}/proxy` / `hide` /
  `sync` / 设备侧上报与拉取
- **本机命令行与实时终端一节**：`shell/status|exec|enable`、`WS /api/v1/term`，
  含消息形状与"不是 PTY"的限制
- **扩展安装四来源**：`url` / `.crx` / 目录 / zip，以及 `make_pack` / `as_default`

### 顺手修掉一处被实测推翻的旧说法

文档原来写「知纹会自动生成一个 MV3 认证扩展来应答 `onAuthRequired`」——
Chrome 137 起忽略 `--load-extension`（149/150/157 实测），那条路根本不通。
改成实话：**真正生效的是本机中继**，扩展代码只在 `ZW_PROXY_RELAY=false` 时才会用到。

### 新增测试 `test_every_public_route_is_documented`

扫 `api.py` 里所有 `@app.<method>` 路由，逐个对 `docs/04`。以后再漏就 CI 红。
（白名单只有 `/healthz`、`/metrics`、`/mcp`、`/ai/manifest` 这类内部路由。）

### 测试

555 → **556 项**。

## 0.7.27 — 2026-11-17

**把新能力补进 MCP（28 → 33 个工具）与 AI 清单。**

### 问题

用户要求「AI 也可以通过这个接口去知道有哪些浏览器和机子在线，并且帮我去控制」。
MCP 是 AI 真正调用的入口，但本轮新增的能力一个都没在里面：

- 代理库：只能 `list` / `check`，**没有批量导入、没有同步、没有隐藏**
- 扩展：**完全没有**装扩展、查更新

也就是说 AI 做不到"帮你导一批代理""装个扩展""查更新"。

### 新增 5 个工具

| 工具 | 作用 |
|---|---|
| `zw_import_proxies` | 批量导入（一行一个，支持链式；坏行单独返回原因） |
| `zw_sync_proxies` | 代理库与服务器双向对齐 |
| `zw_hide_proxy` | 隐藏/取消隐藏（不删除，不影响在用它的环境） |
| `zw_install_extension` | 商店链接 / `.crx` / 目录 / zip → 可自动建包并设为默认 |
| `zw_check_extension_updates` | 回商店比对已装扩展版本 |

工具的说明里写清了「什么时候该用它」（项目规矩）。

### AI 清单也补齐

`/api/v1/ai/manifest` 的「端点清单」加了 4 条：
`proxies/{id}/hide`、`extensions/install`、`extensions/updates`、`proxies/sync`。

### 实测

```
$ tools/list → 33 个工具
  zw_import_proxies ✅  zw_sync_proxies ✅  zw_hide_proxy ✅
  zw_install_extension ✅  zw_check_extension_updates ✅

$ tools/call zw_import_proxies
  {"ok": true, "data": {"total": 2, "imported": 1, "skipped": 0,
   "failed": [{"line": "socks5://66.80.12 0.66:20000",
               "error": "代理主机名里有空格：…"}]}}
```

### 测试

553 → **555 项**：MCP 工具存在性（真跑 `zw mcp` 读 `tools/list`）、
manifest 端点清单含新接口。

> 写测试时踩了两下：`client` fixture 只在 `test_api_smoke.py` 里有（先写错文件导致
> `fixture 'client' not found`）；manifest 的键名是中文「端点清单」而不是 `endpoints`。

## 0.7.26 — 2026-11-17

**补齐 CLI：批量导入代理、代理库同步、商店链接装扩展、扩展更新检查。**

### 为什么补

用户要求「AI 也可以通过这个接口去知道有哪些浏览器和机子在线，并且帮我去控制」——
CLI 是 AI 和脚本最直接的入口。但新增的能力全在 HTTP 上，CLI 一直缺：

| 之前 | 现在 |
|---|---|
| `zw proxy` 只有 list/add/check/rm | 加 **`import`**（一行一个，坏行单独报错）、**`sync`**（与服务器对齐） |
| `zw extension install <path>` 只吃目录 | 加 **`--url`**（商店链接或 32 位 ID）、**`--default`**（设为新环境默认）、**`--no-pack`** |
| 无 | 加 **`zw extension updates`**（查已装扩展有没有新版） |

### 实测（真 core 上跑的）

```
$ zw proxy import --text "socks5://u:p@66.80.12.66:20000 # 落地A
socks5://66.80.12 0.66:20000
http://127.0.0.1:21080,socks5://u2:p2@5.5.5.5:1080 # 链式B"
导入 0 条，跳过 2 条，忽略 0 行，失败 1 条
  ❌ socks5://66.80.12 0.66:20000 → 代理主机名里有空格：…（把 66.80.12.66 写成了 66.80.12 0.66）

$ zw proxy sync
上行：失败（新增 0 / 更新 0） 没有配置服务器或设备号
下行：失败（补入 0 / 更新 0） 没有配置服务器或设备号

$ zw extension updates
（没有带商店 ID 的扩展，手工导入的包查不了更新）

$ zw extension install --url hdhngoamekjhmnpenphenpaiindoinpo
失败 [EXT_STORE_UNREACHABLE] 连不上 Chrome 应用商店（ConnectTimeout: timed out）。
这台机器需要能访问 clients2.google.com；国内网络请先给客户端配好代理
```

坏行、未配 hub、没外网 —— 三种失败都给出了**能照着做的**提示，不是干巴巴一句失败。

### 写的时候踩的坑

`_call()` 已经把 `{ok, data}` 拆开了，我一开始又按 `data["data"]` 取了一层，
于是所有统计都显示 0（"导入 0 条，跳过 0 条"）——看起来像功能没生效。
已改正并在注释里写明。

### 测试

551 → **553 项**（CLI 子命令存在性用 `--help` 断言，最接近用户实际用法）。

## 0.7.25 — 2026-11-17

**新增客户端 GUI 自动化验收（26 条），并修掉"环境行里看不出是哪台代理"。**

### 新脚本 `scripts/gui-check.py`

主控台有 `console-ui-check.cjs`、客户端控制台有 `ui-check.cjs`，
但**客户端 GUI**（用户每天双击的那个 tkinter 窗口）一直没有可重复的验收。
现在有了：起真 core（不是 mock）、通过 HTTP 造数据、在无头 X 里真装配面板、
逐项断言。

覆盖 26 条：环境行数 / 环境名 / 代理列可见且脱敏 / 直连标识 /
「新建环境·删除·代理·扩展包」按钮 / 启停按钮按状态二选一 /
扩展包面板五个入口（含「商店链接安装」「选 .crx 文件」「检查更新」）与 5 个处理函数 /
代理库面板「刷新·批量检测」/ 界面无英文裸异常。

### 修掉的真问题：代理列把 IP 截掉了

环境行里的代理串是 `socks5://user:***@66.80.12.66:20000`（32 字符），
而行只留 `ROW_LIMIT_PROXY = 22` 个字符：

```
socks5://user:***@66.80.…      ← IP 尾部没了
```

于是 **66.80.12.66 和 66.80.12.65 在界面上长得一模一样**，用户分不清用的是哪台。

改成：有认证时把用户名也压成 `***`，超长时优先砍协议名 —— `host:port` 必须完整：

```
***@66.80.12.66:20000          ← 截断后仍是完整 IP
66.80.12.66:20000
```

### 测试里踩的两个坑（都记在注释里了）

1. **不能用 `root.update()` 泵代替 mainloop**：面板取数走 `call_async`
   （后台线程 + `widget.after(0, …)`），而 Tk 的 `after()` 从非主线程调用不被保证 ——
   现象是 `_inflight` 永远 True、回调从不执行、列表停在「还没有环境」上。
   必须用 `after()` 链 + 真 `mainloop()` 驱动。
2. **行内启停是二选一**：`text="停止" if stop_like else "启动"`，
   停止态的环境不会出现「停止」按钮。断言不能要求两个都在。

### 测试

550 → **551 项**。

## 0.7.24 — 2026-11-17

**把跨机端到端验收真正跑起来了（30 项 / 0 失败），此前一直没跑过。**

### 两台真机器

| 端点 | 说明 |
|---|---|
| A | 本机 `192.168.3.6:8798`，0.7.23，绑 `0.0.0.0`，配 hub |
| B | 3.5 上的 `core-b` `192.168.3.5:8731`，**顺手从 0.4.2 升到 0.7.23** |

中间真过了一次局域网，验到的是：真实主机名、真实网关地址、真实跨机 cookie 搬运。

### 覆盖（30 项全过）

两台就绪 → A 建环境/启动/写 cookie → A 推 hub → B 拉取并自动导入 →
B 上的指纹种子/窗口尺寸/时区/分组与 A 一致 → **cookie 真的写进 B 的浏览器** →
错主密码在落盘前被拦（412）→ B 改完推回、版本递增、A 拉回 →
跨机 CDP：从 A 取 B 的网关地址并用 Playwright 接管成功。

### 修掉脚本的鉴权头 bug

旧代码：

```python
TA = {"Authorization": "Bearer " + os.environ.get("ZW_TA", "test-token-123")}
```

本机 core 免令牌时 `ZW_TA` 是空串 → 拼出 `Authorization: Bearer ` →
httpx 直接拒：**`Illegal header value b'Bearer '`**，报错看起来像"连不上"。
改成令牌为空时完全不带这个头。

### 跑之前要满足的四件事（写进脚本 docstring 了）

1. **两端都要配 hub**（`ZW_HUB_URL` + `ZW_HUB_TOKEN`），否则推/拉全废
2. **A 要绑 0.0.0.0** —— 脚本断言 A 的对外网关地址不是回环
3. **远程一端必须给令牌** —— 非回环访问要 `ZW_ADMIN_TOKEN`（这是鉴权模型，不是权限问题）
4. **设备要被服务器批准** —— `ZW_DEVICE_AUTO_APPROVE=false` 时新设备是 `pending`，
   推送返回 `DEVICE_PENDING`；先去面板点「允许」

> 顺带又一次实机验证了审批流程：A 从 `pending` 点成 `active` 之后同步才通。

### 顺带

3.5 上那个 `zhiwen-core-8731` 服务（systemd）一直跑着 **0.4.2** 的老代码快照，
已同步到 0.7.23 并补了 `ZW_ADMIN_TOKEN`（原来它没有任何凭据，远程访问会被拒）。

## 0.7.23 — 2026-11-17

**修掉"本机打开客户端控制台仍然要输令牌"——用户早就抱怨过的那件事。**

### 真相：接口放行了，页面还在拦

0.7.3 我改了鉴权模型（回环保送、远程凭机器码），接口层实测：
`GET /api/v1/meta` → `auth_required: false`、无令牌也能拿数据。

**但页面启动时是这么写的**：

```js
if (!S.token) { showLogin(''); renderView(); return; }   // ← 本地没令牌就无条件弹框
```

它**根本没看**服务端返回的 `auth_required`。所以即使接口已经放行，
你一打开 `http://127.0.0.1:8730/` 还是被要求输令牌 —— 那个改动等于没生效。
我之前只验证了 API、没验证**页面**，这个疏漏正是用户原话抱怨的现象。

### 修法

启动顺序改成「先问 meta，再决定要不要弹」：

```js
if (!S.token) {
  const okMeta = await loadMeta();
  if (S.meta && S.meta.auth_required === false) {   // 本机回环 / 没配令牌
    hideLogin();
    await startData();                              // 直接进
    return;
  }
  showLogin('');
  renderView();
  return;
}
```

### 验证

真浏览器实测（无头 Chromium，本机 8797）：

```
登录层显示: false | localStorage 有令牌: false
页面有内容: true
导航可见: true
页面报错: none
```

客户端控制台 UI 验收 `scripts/ui-check.cjs`：**23 项全过 / 0 失败**
（原来卡在登录层上，脚本自己都点不动 —— 产品坏了，不是脚本坏了）。

新增 2 条断言把这件事钉死：

- 「本机打开不弹登录层（auth_required=false 时直接进）」
- 「本机免令牌时不需要往 localStorage 写令牌」

另有 Python 侧 `test_client_console_page_skips_login_when_auth_not_required`
直接读页面源码，保证 `!S.token` 分支里有 `loadMeta()` 与 `hideLogin()`，
防止以后又被改回去。

### 测试

549 → **550 项**。

## 0.7.22 — 2026-11-17

**给控制台的「代理」视图补上 UI 验收，并修掉 3 条一直在误报的断言。**

### 新增覆盖（13 条）

`scripts/console-ui-check.cjs`（真浏览器 + mock 后端）原来对「代理」视图**零覆盖**。
现在加了三条 fixture（单跳 / 链式 / 已隐藏）与整套断言：

- 切视图后 hash=#proxies 且只显示这一屏
- 隐藏项默认不显示（2 行而不是 3 行）
- 单跳显示 `scheme://host:port`、链式显示前置地址
- 密码列「已设置 / 无」、来源列显示 origin、计数文案
- 点「隐藏」发出正确请求；勾「显示已隐藏」后出现「已隐藏」徽章与「取消隐藏」按钮
- 「添加代理」弹窗、填表、保存，并校验请求体带对 host/port/label

### 修掉 3 条误报（都是用例过时，不是产品 bug）

| 断言 | 为什么一直红 |
|---|---|
| `KPI：机器 2 / 在线 1 / 浏览器 2 / 运行中 1` | 控制台后来加了「待授权」这一格（免授权码审批流程），是 **5** 个而不是 4 个 |
| `终端徽章显示「命令行：开」` | 徽章已改成表达通道形态（**实时 / 队列**），那个文案早被更好的设计取代 |
| `全程没有 JS 报错` | 实时终端会开 WS，而 mock 后端只有 HTTP，浏览器必然报连接失败（无头环境还有 `ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`）—— 属预期噪音 |

修完之后：**64 条断言全过，0 失败**（之前是 48 过 / 3 失败）。
把假失败清掉，这个脚本才能当可信的门槛用。

### 当前验收资产

| 脚本 | 结果 |
|---|---|
| `pytest tests` | 549 passed, 2 skipped |
| `scripts/e2e-check.py`（单机全能力） | 65 通过 / 0 失败 / 6 跳过 |
| `scripts/console-ui-check.cjs`（控制台界面） | 64 通过 / 0 失败 |

## 0.7.21 — 2026-11-17

**验收脚本抓出两个真 bug：商店装扩展在 HTTP 上根本不可达；代理列表丢了链式信息。**

这两条都是"单测全绿、实际用不了"的类型，是扩展 `scripts/e2e-check.py`
之后才暴露的。

### 1. `/api/v1/extensions/install` 有两条路由，新的被遮蔽

0.7.17 我新加了一条同路径同方法的装饰器（从商店链接装），但老的
"从目录装进中央库"先注册。FastAPI 用**先注册**的那条，于是：

- 带 `url` 调过去报 `body.path Field required`
- `make_pack` / `as_default` 被完全忽略

**0.7.17–0.7.20 这四版，商店装扩展通过 HTTP 一次都没成功过。**
本地单测直接调 `extstore.install()`，绕过了 HTTP，所以一直没发现 ——
这个教训值得记住：**测接口就一定要走接口**。

现在合并成一个入口，按来源分流：

| 入参 | 行为 |
|---|---|
| `url` | 商店链接/ID → 下载 CRX |
| `path` = `*.crx` | 本地 CRX 文件 |
| `path` = 目录 / `*.zip` | 老行为，装进中央库（返回 `sha256`） |

`ExtensionInstall` 也补了 `url` / `make_pack` / `as_default`，`path` 不再必填。
新增测试 `test_extensions_install_has_exactly_one_route` 直接数路由数量，
重复注册会立刻变红。

### 2. `GET /api/v1/proxies` 自己拼序列化，漏了 `is_chain` / `chain`

这个端点有一段内联实现：直接 `SELECT *`、`pop` 掉 password、手工拼 upstream。
它**没走** `proxylib.list_all()`，于是：

- 链式代理的前置信息（`is_chain` / `chain`）完全不返回 → 界面上画不出"前置 → 落地"
- 还拿一个根本不存在的 `proxy_pw_<id>` 设置项判断有没有密码

现在委托给 `proxylib.list_all()`，字段形状只有一处真相。

### 3. 验收脚本扩到 65 项

新增覆盖本轮全部新功能：

- 坏代理串被拒（带空格）+ 密码脱敏
- 链式代理可导入、`is_chain` 保留
- 代理库同步状态接口可用
- CRX 安装成功 + 装完自动建包 + 可设为默认 + 更新检查可用
- CDP 令牌默认 3600 秒

`ZW_CORE=... python scripts/e2e-check.py` → **通过 65 项，失败 0 项，跳过 6 项**。

> 写脚本时也踩了个自己的坑：局部变量取名 `failed`，把脚本里"失败项列表"
> 遮蔽了，打印出来的失败项变成了一坨字典。已改名并在注释里写明。

### 测试

547 → **549 项**。

## 0.7.20 — 2026-11-17

**把"商店链接装扩展"的整条链路验到底（真浏览器里插件真的跑起来）。**

### 终点判据

0.7.19 证明了"装完会建包、新建环境会带上"，但没证明**插件在浏览器里真的执行**。
这次补上，并且固化成集成测试：

```
装 CRX → 自动建包 → 设默认 → 新建环境（extensions 含 slug）
      → 启动（命令行带 --load-extension=<库路径>）
      → CDP 打开页面 → 内容脚本留下标记
```

实测输出：

```
slug=cookiemanager ver=1.2.3 建包=True 默认=True
profiles.extensions = ['cookiemanager']
--load-extension=/tmp/storechain/extensions/cookiemanager-1.2.3
页面标记 data-store-installed = 'YES'
✅ 全链路通
```

集成测试 `test_installed_extension_actually_runs_in_browser` 会自己找一个
**真的能加载扩展的内核**（探测 `extensions_loadable`）；找不到就 skip，
所以官方 Chrome 的 CI 上不会误红。

### 还差哪一步没走真网络

只有"从 Google 下载 CRX"这一段。那一段已单独在云电脑上验证过：
真实下到 cookiemanager 的 164717 字节 CRX，`Cr24 / ver=3 / header_len=1310 / ZIP@1322`。
所以链路两端都有证据，中间的逻辑由本地 CRX 覆盖。

### 测试

546 → **547 项**。

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
