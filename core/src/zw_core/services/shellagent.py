"""远程命令行的**设备侧代理**：从 hub 拉命令、本地执行、回传结果。

## 为什么是拉取

客户端可能在云电脑/NAT 后面，hub 连不进去它的 127.0.0.1。
所以这里是**纯出站**的两步循环：

```
GET  /v1/devices/shell/next?wait=25   → 拿到一条命令（没有就空转一轮）
POST /v1/devices/shell/{id}/result    → 回传 rc/stdout/stderr
```

设备**不新开任何监听端口** —— 这是这个设计最重要的安全属性。

## 关闭是怎么生效的

两道：

1. 开关关掉时，这个循环**根本不发 next 请求**，命令永远到不了设备。
2. 即使已经拿到命令、执行前开关被关掉，也在本地再查一次并回
   `error="SHELL_DISABLED"`（纵深防御）。

第二道是必要的：开关可能在"拿到命令之后、执行之前"被改。
只靠第一道的话，用户关掉开关后还会看到命令被执行一次，那是很吓人的体验。

## 断线与退避

- 网络失败按 1s → 2s → 5s → 10s → 30s 退避，别把 hub 打爆。
- 被踢（`DEVICE_REVOKED`）就**停止拉取**并等恢复；继续拉只会刷错误日志。
- 心跳成功说明网络是通的，退避可以复位（由 `notify_online` 触发）。
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from typing import Any

logger = logging.getLogger("zw.shellagent")

_RETRY_STEPS = (1.0, 2.0, 5.0, 10.0, 30.0)


class ShellAgent:
    """后台线程：长轮询 hub，执行命令，回传结果。"""

    def __init__(self, hub_url: str, device_id: str, token: str = "", wait: int = 25) -> None:
        self.hub_url = (hub_url or "").rstrip("/")
        self.device_id = device_id or ""
        self.token = token or ""
        self.wait = max(1, min(int(wait or 25), 30))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = True
        self._retry = 0
        # 统计（给界面/自检看）
        self.executed = 0
        self.failed = 0
        self.last_cmd = ""
        self.last_rc: int | None = None
        self.last_error = ""
        self.last_at = 0.0
        self.paused_reason = ""

    # ── 生命周期 ────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.hub_url or not self.device_id:
            self.paused_reason = "没有 hub 地址或设备号"
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="zw-shell", daemon=True)
        self._thread.start()
        logger.info("远程命令行代理已启动 device=%s", self.device_id)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)
        self._thread = None

    def set_enabled(self, enabled: bool) -> None:
        """运行时开关。关掉时循环会尽快停下（最长等一个 wait 周期）。"""
        self._enabled = bool(enabled)
        logger.info("远程命令行开关 → %s", "开" if self._enabled else "关")

    def set_hub(self, hub_url: str, device_id: str = "", token: str = "") -> None:
        """服务器地址变了：重启循环指向新地址。"""
        self.hub_url = (hub_url or "").rstrip("/")
        if device_id:
            self.device_id = device_id
        if token or token == "":
            self.token = token
        self.stop()
        self._retry = 0
        self.paused_reason = ""
        if self.hub_url and self.device_id:
            self.start()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "hub": self.hub_url,
            "device_id": self.device_id,
            "executed": self.executed,
            "failed": self.failed,
            "last_command": self.last_cmd,
            "last_rc": self.last_rc,
            "last_error": self.last_error,
            "last_at": self.last_at,
            "paused_reason": self.paused_reason,
        }

    # ── 主循环 ──────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._enabled:
                # 关着的时候什么都不做，只等开关变化（不要空转打 hub）
                if self._stop.wait(1.0):
                    return
                continue
            try:
                job = self._fetch_one()
            except _Revoked:
                self.paused_reason = "已被服务器移除"
                logger.warning("远程命令行暂停：设备已被服务器移除")
                # 被踢后不做重试风暴，等 set_enabled/set_hub 或恢复
                if self._stop.wait(30.0):
                    return
                continue
            except Exception as exc:  # noqa: BLE001  网络异常一律退避重试
                self.last_error = f"{type(exc).__name__}: {exc}"
                delay = _RETRY_STEPS[min(self._retry, len(_RETRY_STEPS) - 1)]
                self._retry += 1
                logger.debug("拉取命令失败，%.0fs 后重试：%s", delay, exc)
                if self._stop.wait(delay):
                    return
                continue

            self._retry = 0
            if not job:
                continue
            self._handle(job)

    def _handle(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        command = str(job.get("command") or "")
        if not job_id:
            return

        # 纵深防御：拿到命令后、执行前再查一次开关
        if not self._enabled:
            self._report(job_id, {
                "rc": -1, "stdout": "", "stderr": "", "duration_ms": 0,
                "truncated": False, "error": "SHELL_DISABLED: 这台电脑的远程命令行已关闭",
            })
            return

        from zw_core.services import shell as shellsvc

        result = shellsvc.execute(
            command,
            cwd=str(job.get("cwd") or ""),
            timeout=job.get("timeout"),
        )
        self.executed += 1
        self.last_cmd = command
        self.last_rc = result.rc
        self.last_at = time.time()
        if result.error:
            self.failed += 1
            self.last_error = result.error

        self._report(job_id, {
            "rc": result.rc,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
            "error": result.error,
        })

    # ── HTTP ────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        h = {"X-ZW-Device": self.device_id}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _fetch_one(self) -> dict[str, Any] | None:
        import httpx

        url = f"{self.hub_url}/v1/devices/shell/next"
        with httpx.Client(timeout=self.wait + 15) as c:
            r = c.get(url, params={"wait": self.wait}, headers=self._headers())
        if r.status_code == 403:
            code = ""
            try:
                code = (r.json() or {}).get("code", "")
            except Exception:
                pass
            if code in ("DEVICE_REVOKED", "DEVICE_UNKNOWN"):
                raise _Revoked(code)
        if r.status_code >= 400:
            raise RuntimeError(f"hub 返回 {r.status_code}: {r.text[:200]}")
        data = (r.json() or {}).get("data") or {}
        job = data.get("job")
        return job if isinstance(job, dict) else None

    def _report(self, job_id: str, payload: dict[str, Any]) -> None:
        import httpx

        url = f"{self.hub_url}/v1/devices/shell/{job_id}/result"
        try:
            with httpx.Client(timeout=20) as c:
                r = c.post(url, json=payload, headers=self._headers())
            if r.status_code >= 400:
                self.last_error = f"回传失败 {r.status_code}: {r.text[:160]}"
                logger.warning("命令结果回传失败 job=%s %s", job_id, r.status_code)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"回传异常：{type(exc).__name__}: {exc}"
            logger.warning("命令结果回传异常 job=%s：%s", job_id, exc)


def ensure_registered(hub_url: str, device_id: str, token: str = "", app_version: str = "") -> dict[str, Any]:
    """让本机 core 在 hub 上注册成设备。

    客户端（GUI）连上时会显式注册；但"服务器上直接跑的 core"
    （例如 3.5 自己的那个）没有客户端，如果不注册，心跳与命令都会
    403 DEVICE_UNKNOWN —— 表现为"面板里看不到这台机器"。

    这里在启动时补一次注册。已经注册过的设备重复注册是幂等的（hub 会刷新信息），
    被踢下线的设备**不会**因此复活（hub 侧明确拒绝并在注册里返回 DEVICE_REVOKED）。
    """
    if not hub_url or not device_id:
        return {"ok": False, "error": "缺少 hub 地址或设备号"}
    try:
        import httpx

        from zw_core import __version__

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "device_id": device_id,
            "app_version": app_version or __version__,
            "hostname": socket.gethostname(),
            "platform": sys.platform,
        }
        with httpx.Client(timeout=12) as c:
            r = c.post(f"{hub_url.rstrip('/')}/v1/devices/enroll", json=payload, headers=headers)
        body = r.json() if r.content else {}
        if r.status_code == 200 and body.get("ok"):
            return {"ok": True, **(body.get("data") or {})}
        return {"ok": False, "code": body.get("code") or f"HTTP_{r.status_code}",
                "error": body.get("message") or r.text[:200]}
    except Exception as exc:  # noqa: BLE001  注册失败不该拖垮 core 启动
        return {"ok": False, "code": "UNREACHABLE", "error": f"{type(exc).__name__}: {exc}"}


class _Revoked(Exception):
    """设备已被服务器移除。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HeartbeatThread:
    """无客户端时的心跳。

    GUI 客户端有自己的心跳线程（它还负责"被踢"的界面提示）。但服务器上
    直接跑的 core 没有客户端 —— 不自己发心跳的话，注册后 3 分钟就会被
    hub 判定离线，面板里一直是灰的，也没人上报 shell_enabled。

    这里的心跳同时捎带本机概况（环境数/运行中/磁盘/引擎），
    让面板能看到 3.5 自己在干什么。失败只记 debug：网络抖一下不值得刷日志。
    """

    def __init__(self, hub_url: str, device_id: str, token: str = "",
                 interval: int = 60, enabled: bool = True) -> None:
        self.hub_url = (hub_url or "").rstrip("/")
        self.device_id = device_id or ""
        self.token = token or ""
        self.interval = max(15, int(interval or 60))
        self.enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ok = 0.0
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.hub_url or not self.device_id:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="zw-hb", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)
        self._thread = None

    def status(self) -> dict[str, Any]:
        return {"running": bool(self._thread and self._thread.is_alive()),
                "last_ok": self.last_ok, "interval": self.interval,
                "last_error": self.last_error}

    def _payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"shell_enabled": self.enabled}
        try:
            from zw_core.services import manager as mgr

            st = mgr.stats()
            out.update({
                "profiles": int(st.get("profiles") or 0),
                "running": int(st.get("running") or 0),
                "max_running": int(st.get("max_running") or 0),
                "disk_human": str(st.get("disk_human") or ""),
                "engine": str((st.get("engine") or {}).get("version") or ""),
            })
        except Exception:
            pass

        # 主控台要展示"这台有哪些浏览器、CDP 地址是什么"，所以把清单也带上。
        # 只报元信息（id/名字/状态/分组），**绝不带 cookie、密码、种子**。
        try:
            out["core_base"] = self._core_base()
            out["inventory"] = self._inventory()
        except Exception:
            pass
        return out

    def _core_base(self) -> str:
        """本机 core 的对外地址，主控台据此拼 CDP 链接。

        优先用配置里的 `gateway_public_base`（已经考虑端口与对外 IP）。
        NAT 后面的机器这个地址外面连不上 —— 主控台会标"不可直连"，
        这不是错误，用户还是能通过远程终端在机器上操作。
        """
        from zw_core.config import get_settings

        return get_settings().gateway_public_base.rstrip("/")

    def _inventory(self) -> list[dict[str, Any]]:
        """当前浏览器环境清单（给主控台展示用）。

        每个环境带上**脱敏的**代理摘要：主控台要能显示"这个环境走哪个代理"，
        但代理密码不该离开这台机器（见 proxylib.display_summary 的说明）。
        """
        from zw_core.services import manager as mgr, proxylib

        items: list[dict[str, Any]] = []
        for p in mgr.list_profiles():
            pid = str(p.get("id") or "")
            spec = None
            raw_proxy = str(p.get("proxy") or "").strip()
            problem = ""
            try:
                spec = mgr.proxy_spec_for(p)
            except Exception as exc:
                # 代理串是坏的（带空格 / IP 段数不对 / 端口越界…）。
                # 把原因一起报给主控台，让它显示「代理无效」+ 具体问题，
                # 而不是让用户去点启动才发现。
                problem = str(getattr(exc, "message", "") or exc)
                spec = None
            items.append({
                "id": pid,
                "name": str(p.get("name") or ""),
                "state": str(p.get("state") or ""),
                "group": str(p.get("group_name") or ""),
                "cdp_path": f"/cdp/{pid}" if pid else "",
                "proxy": proxylib.display_summary(spec, raw=raw_proxy, problem=problem),
            })
        return items

    def send_now(self) -> bool:
        """立刻发一次心跳（不等下一个周期）。

        两个用处：
        1. 用户切换"允许远程命令行"后，面板立刻能看到新状态，
           而不是等最多 60 秒 —— 那种延迟会让人以为开关没生效。
        2. 测试与自检里可以直接验一次往返，不用等定时器。
        """
        import httpx

        if not self.hub_url or not self.device_id:
            return False
        headers = {"X-ZW-Device": self.device_id}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(f"{self.hub_url}/v1/devices/heartbeat",
                           json=self._payload(), headers=headers)
            if r.status_code == 200:
                self.last_ok = time.time()
                self.last_error = ""
                return True
            self.last_error = f"HTTP {r.status_code}"
            logger.debug("心跳被拒：%s %s", r.status_code, r.text[:120])
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("心跳失败：%s", exc)
            return False

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.send_now()


# 全局单例：core 在 lifespan 里启动它，API 层读它的状态
_AGENT: ShellAgent | None = None
_HB: HeartbeatThread | None = None
_LOCK = threading.Lock()


def get_agent() -> ShellAgent | None:
    return _AGENT


def start_agent(hub_url: str, device_id: str, token: str = "", enabled: bool = True) -> ShellAgent | None:
    """启动全局代理（幂等）。没有 hub 地址或设备号就不启动。"""
    global _AGENT
    if not hub_url or not device_id:
        return None
    with _LOCK:
        if _AGENT is None:
            _AGENT = ShellAgent(hub_url, device_id, token)
        else:
            _AGENT.set_hub(hub_url, device_id, token)
        _AGENT.set_enabled(enabled)
        _AGENT.start()
        return _AGENT


def start_heartbeat(hub_url: str, device_id: str, token: str = "",
                    enabled: bool = True, interval: int = 60) -> HeartbeatThread | None:
    """启动全局心跳（幂等）。没有 hub 地址或设备号就不启动。"""
    global _HB
    if not hub_url or not device_id:
        return None
    with _LOCK:
        if _HB is None:
            _HB = HeartbeatThread(hub_url, device_id, token, interval=interval, enabled=enabled)
        else:
            _HB.hub_url = hub_url.rstrip("/")
            _HB.device_id = device_id
            _HB.token = token
            _HB.enabled = enabled
        _HB.start()
        return _HB


def get_heartbeat() -> HeartbeatThread | None:
    return _HB


def stop_agent() -> None:
    global _AGENT, _HB
    with _LOCK:
        if _AGENT is not None:
            _AGENT.stop()
            _AGENT = None
        if _HB is not None:
            _HB.stop()
            _HB = None
