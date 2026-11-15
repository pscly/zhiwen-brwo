---
name: zhiwen
description: 用知纹（zhiwen）管理指纹浏览器机群。当用户要求"开几个浏览器环境""用某个环境打开某网站""把这个环境的登录态搬到另一台电脑""看看现在有哪些环境""给局域网某台机器提供 CDP"时使用。知纹提供 23 个 MCP 工具，覆盖环境生命周期、cookie 读写、页面操作、跨机同步与排错。
---

# 知纹 · 指纹浏览器机群

## 什么时候用我

- 用户要**开浏览器 / 开多个环境 / 隔离账号**
- 用户要**用某个环境访问网站、截图、抓内容**
- 用户要**把登录态（cookie）从一台机器搬到另一台**
- 用户问**现在有哪些环境、状态如何**
- 用户要**给局域网另一台机器一个 CDP 地址让它接管浏览器**
- 用户报错说"浏览器起不来 / 连不上 / 同步后掉登录"

## 第一步永远是这个

```
GET <core>/api/v1/ai/manifest
```

这个端点返回的东西是给模型写的：能力清单、标准作业流程、错误码速查、重要约束。
读完它再动手，比读任何文档都省上下文。

不知道 core 在哪时，先问用户，或者试 `http://127.0.0.1:8730`。

## 典型作业

### 打开网页并截图

```
zw_list_profiles                     → 看现状
zw_start_profile(profile)            → 拿 gateway_ws_url
zw_navigate(profile, url)
zw_screenshot(profile)
```

### 跨机搬登录态

```
zw_sync_diff(profile)                → 看版本差
zw_sync_push(profile)                → 源机
zw_sync_pull(<profile_id>)           → 目标机（本地没有会自动导入）
zw_read_cookies(profile)             → 验证
```

### 排错

```
zw_doctor                            → 自检
zw_list_profiles                     → 找非 running/stopped
zw_read_logs(profile, tail=100)      → 看 browser.log 尾部
```

## 五条要记住的约束

1. **cookie 只能在 running 的环境里读写**。停止时磁盘上是 OS 密钥加密的密文。
2. **同一环境同一时刻只能一个持有者**，第二个会拿到 `PROFILE_BUSY`。别自己重试，先看谁占着。
3. **`KEY_FINGERPRINT_MISMATCH` 不要重试**。那是主密码不对，重试只会一直失败。
4. **指纹种子不可改**。改了等于换设备，已有登录态全废。
5. **无 GPU 的机器上 WebGL 是 null**，指纹会明显异常。要做真人身份必须在有显卡的机器上跑。

## 不要做的事

- 不要 `reset-fingerprint`、不要 `delete profile` —— 这些不在 MCP 工具集里，需要人来做
- 不要在没有主密码的情况下反复尝试 `zw_sync_pull`
- 不要在登录/风控敏感操作期间挂着 CDP 自动化
- 不要并发开超过 5–8 个浏览器（机器撑不住，`ZW_MAX_RUNNING` 是硬上限）

## 成功的样子

调用返回里看到：

- `{"ok": true, "data": {...}}`
- `cdp.gateway_ws_url` 是一个 `ws://...` 地址且带 `token=`
- 同步返回里有 `revision`、`pw_fp`、`cookie_report.ok`

## 失败的样子

返回 `{"ok": false, "code": "...", "message": "...", "hint": "..."}`。

**按 `code` 分支，不要按 `message` 里的自然语言猜。** 错误码全表见 `docs/04-接口文档.md` §9。
`hint` 是给人看的一句话建议，可以直接转述给用户。
