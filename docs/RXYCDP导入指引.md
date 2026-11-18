# 把 RXYCDP 浏览器的插件（及其数据）导入知纹控制台

> 这份是给**操作者**看的：三步，5 分钟，不需要命令行。

## 前提

- RXYCDP 浏览器上一次**正常关闭**过（运行中导入会漏掉正在写的设置）
- 电脑上已装知纹客户端（≥0.7.66），并能打开控制台

## 第 1 步：找到 RXYCDP 的「User Data」目录

RXYCDP / Roxy 这类浏览器都是 Chromium 内核，它的用户数据在一个叫 **`User Data`** 的
文件夹里，里面有 `Default`（或 `Profile 1`）子目录。

常见位置（把 `你的用户名` 换成实际的）：

```
C:\Users\你的用户名\AppData\Local\RoxyBrowser\User Data
C:\Users\你的用户名\AppData\Local\RXYCDP\User Data
C:\Users\你的用户名\AppData\Roaming\RXYCDP\User Data
D:\RXYCDP\User Data
```

**最快的找法**：在资源管理器地址栏输入

```
%LOCALAPPDATA%
```

回车，然后在右上角搜索框里搜 `User Data`，等它列出来；或者直接搜 `RXYCDP`。

> 判断对不对：进去后能看到 `Default` 文件夹，再进去有 `Extensions` 和
> （可能还有）`Local Extension Settings` —— 那就是它。

## 第 2 步：导入成"扩展包"

**在桌面客户端里**（不是浏览器控制台）：打开「扩展包」面板，点
**「从 Chrome 目录导入」**，把第 1 步那个 `User Data` 目录**整个路径**粘进去
（它默认已经填了 Chrome 的标准位置，改掉即可）。

> 也可以用接口：`POST /api/v1/packs/import`，body 是
> `{"from_chrome": "C:\\Users\\你\\AppData\\Local\\RXYCDP\\User Data", "name": "RXYCDP", "as_default": true}`。
> 路径给到 `User Data` 或 `User Data\\Default` 都认。

返回结果里看三个字段：

| 字段 | 应为 | 含义 |
|---|---|---|
| `extension_count` | > 0 | 导入到几个扩展 |
| `with_data` | 有数据时 > 0 | 有几个扩展带上了设置数据 |
| `is_default` | `true`（若勾了默认） | 新建环境是否自动带上 |

如果 `extension_count` 是 0，说明那个目录里没有第三方扩展 ——
回到第 1 步换个 `Profile N` 目录再试。

## 第 3 步：设成默认（可选）——以后新环境自动带上

导入时带 `as_default`（控制台上是「设为默认」开关），或者在「扩展」页把那个包
点成默认。之后**新建环境会自动带上这些扩展及其数据**。

不想带的环境：在环境详情里取消勾选该包即可（这就是"也可以选择关闭"）。

## 常见问题

**Q：导入后浏览器里看不到插件图标？**
A：这是内核限制，不是导入失败。Chrome 137 起忽略 `--load-extension`，
官方 Chrome 不会加载命令行指定的扩展。扩展的**代码与数据**都已经带进环境了
（换 ungoogled-chromium 内核就会生效）。环境详情里的 `extensions_active`
会如实告诉你到底加载没：`false` = 没加载。

**Q：`extensions_active` 在哪里看？**
A：环境详情接口 `GET /api/v1/profiles/{id}` 里直接有
`extensions_active` 与 `extensions_note`（0.7.61 起）。

**Q：能把整台浏览器的 cookie / 登录态也搬过来吗？**
A：跨机搬 `Default/Cookies` 是禁止的（DPAPI 加密，换机器解不开，
Chromium 还会把解不开的 cookie 当损坏删掉）。扩展和数据可以搬，登录态请在该
环境里重新登录。
