"""core 侧：从控制台（hub）拉扩展包，落到本机。

## 为什么需要它（2026-11-20）

用户要的是「把插件打包放到控制台，以后建环境默认带上」。
hub 那侧已经有存包接口了，但如果 core **不能拉**，这条链就断在最后一米 ——
包躺在服务器上，别的机器还是用不到。

这个模块补的就是最后一米：

```
hub GET /v1/packs              → 列出控制台里有哪些包
hub GET /v1/packs/{id}/download → 下载 zip（带头 X-ZW-Sha256）
校验 sha256 → 解到临时目录 → 走 packs 的导入路径 → 成为本机包
```

## 几条硬约定

- **sha256 必须校验**：下载不完整时宁可报错，也不要把半截包导进本机库。
- **同 id 不重复导**：已经拉过的包记在 `packsync` 的状态里，重复调用不会产生
  一堆一模一样的环境包。
- **失败不影响本机使用**：拉不到就当没有，日志里说清楚，绝不因为服务器不可达
  让建环境失败。
- **不自动设为默认包**：是否默认由用户决定（`as_default` 参数显式开）。
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile
from typing import Any

from .. import config

logger = logging.getLogger("zw.packsync")

#: 上次同步的结果，给界面/接口读
_LAST: dict[str, Any] = {
    "at": 0.0,
    "ok": None,
    "added": [],
    "skipped": [],
    "error": "",
}

#: 已经拉过的包 id（持久化到数据目录，避免每次重启重复下载）
_STATE_FILE = "packsync.json"


def last_result() -> dict[str, Any]:
    return dict(_LAST)


def _hub() -> tuple[str, str, str]:
    s = config.get_settings()
    return (str(getattr(s, "hub_url", "") or "").rstrip("/"),
            str(getattr(s, "device_id", "") or ""),
            str(getattr(s, "hub_token", "") or ""))


def _headers(dev: str, token: str) -> dict[str, str]:
    h = {"X-ZW-Device": dev}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _state_path():
    return config.get_settings().root / _STATE_FILE


def _load_state() -> dict[str, Any]:
    import json

    p = _state_path()
    if not p.is_file():
        return {"pulled": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"pulled": {}}
    except (OSError, ValueError):
        return {"pulled": {}}


def _save_state(st: dict[str, Any]) -> None:
    import json

    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("写同步状态失败（不影响本次拉取）：%s", exc)


def _err_of(r) -> str:
    try:
        body = r.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            return f"{detail.get('code', '')} {detail.get('message', '')}".strip()
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP {r.status_code}"


def pull(*, timeout: float = 60.0, as_default: bool = False) -> dict[str, Any]:
    """从控制台拉取扩展包并导入本机。

    返回 `{ok, added:[包名], skipped:[原因], error}`。
    """
    hub, dev, token = _hub()
    if not hub or not dev:
        return {"ok": False, "skipped": ["没有配置服务器或设备号"], "added": [], "error": ""}

    import httpx
    from . import packs

    # **先确保这台机器在服务器上注册过**（2026-11-20 实测踩到）。
    #
    # 带 `X-ZW-Device` 访问时，hub 会 `require_active(dev)`；设备没注册就回
    # 404 `DEVICE_UNKNOWN`。首次运行（或服务器换过库）时这一定会发生 ——
    # 表现是"拉取失败: HTTP 404"，看起来像接口不存在，实际是设备没注册。
    #
    # 跟 `proxysync` 保持一致：先补一次注册，失败不拦（注册接口本身可能被 CIDR 限制）。
    try:
        from . import shellagent

        shellagent.ensure_registered(hub, dev, token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("拉包前注册设备失败（继续尝试）：%s", exc)

    added: list[str] = []
    skipped: list[str] = []
    try:
        r = httpx.get(f"{hub}/v1/packs", headers=_headers(dev, token), timeout=timeout)
        if r.status_code == 404 and "DEVICE_UNKNOWN" in (r.text or ""):
            # 再试一次注册（可能是刚注册还没生效，或注册被跳过了）
            try:
                from . import shellagent

                shellagent.ensure_registered(hub, dev, token)
                r = httpx.get(f"{hub}/v1/packs", headers=_headers(dev, token), timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        if r.status_code != 200:
            raise RuntimeError(_err_of(r))
        remote = ((r.json() or {}).get("data") or {}).get("packs") or []
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        _LAST.update(at=time.time(), ok=False, added=[], skipped=[], error=msg)
        logger.warning("扩展包下行失败（不影响本机使用）：%s", msg)
        return {"ok": False, "added": [], "skipped": [], "error": msg}

    st = _load_state()
    pulled: dict[str, Any] = st.setdefault("pulled", {})

    for item in remote:
        pid = str(item.get("id") or "")
        name = str(item.get("name") or pid)
        want = str(item.get("sha256") or "")
        if not pid:
            continue
        # 已经拉过、且服务器上的 sha 没变 → 跳过（不重复建包）。
        #
        # **但必须先确认本机的那个包还在**（2026-11-20 审出来的坑）：
        # 用户删掉本机包之后再来拉，这里只比对 sha 就会判"已是最新"直接跳过，
        # 于是**永远拉不回来**，而返回里还是 `ok=True` —— GUI 显示"没有新包"。
        # 用户以为拉成功，实际一个包都没有。
        if pid in pulled and pulled[pid].get("sha256") == want:
            local_id = str(pulled[pid].get("local_id") or "")
            if local_id and _local_pack_exists(packs, local_id):
                skipped.append(f"{name}（已是最新）")
                continue
            # 本机那份没了 → 当作没拉过，继续往下重新下载
            logger.info("本机包 %s 已不存在，重新拉取 %s", local_id or "?", name)
        try:
            dr = httpx.get(f"{hub}/v1/packs/{pid}/download",
                           headers=_headers(dev, token), timeout=max(timeout, 120.0))
            if dr.status_code != 200:
                raise RuntimeError(_err_of(dr))
            blob = dr.content
            got = hashlib.sha256(blob).hexdigest()
            head_sha = dr.headers.get("X-ZW-Sha256", "")
            if want and got != want:
                raise RuntimeError(
                    f"sha256 不一致（下载 {got[:12]}…，服务器记录 {want[:12]}…）")
            if head_sha and got != head_sha:
                raise RuntimeError(f"响应头 sha256 不一致（{head_sha[:12]}…）")
            info = _import_bytes(packs, blob, name)
            if as_default:
                try:
                    packs.set_default(str(info["id"]), True)
                except Exception as exc:  # noqa: BLE001 —— 设默认失败不该算拉取失败
                    logger.warning("包 %s 拉下来了但设默认失败：%s", name, exc)
            added.append(name)
            pulled[pid] = {"sha256": got, "at": time.time(), "local_id": info["id"]}
        except Exception as exc:  # noqa: BLE001 —— 单个包失败不拖垮其余
            skipped.append(f"{name}（{type(exc).__name__}: {exc}）")
            logger.warning("拉取扩展包 %s 失败：%s", name, exc)

    _save_state(st)
    _LAST.update(at=time.time(), ok=True, added=added, skipped=skipped, error="")
    if added:
        logger.info("从控制台拉取扩展包 %s 个：%s", len(added), "、".join(added))
    return {"ok": True, "added": added, "skipped": skipped, "error": ""}


def push(pack_id: str = "", *, timeout: float = 120.0) -> dict[str, Any]:
    """把本机包上传到控制台。`pack_id` 为空 = 上传全部本机包。

    ## 为什么要它（2026-11-20 审查指出）

    在此之前只有"拉"没有"推"：控制台的上传接口存在，但**客户端没有入口**，
    用户手上根本没有那个 zip。这条路是断的。

    这里用 `packs.export_zip()` 现打 zip 再 POST 到 hub，id 沿用本机 id
    （同 id 在 hub 上已存在会被 CAS 拒，返回里会说明跳过）。
    """
    hub, dev, token = _hub()
    if not hub or not dev:
        return {"ok": False, "pushed": [], "skipped": ["没有配置服务器或设备号"], "error": ""}

    import httpx
    from . import packs

    try:
        from . import shellagent

        shellagent.ensure_registered(hub, dev, token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("上传前注册设备失败（继续尝试）：%s", exc)

    targets = [p for p in packs.list_packs()
               if not pack_id or str(p.get("id")) == str(pack_id)]
    if not targets:
        return {"ok": False, "pushed": [], "skipped": [f"本机没有包：{pack_id or '（全部）'}"], "error": ""}

    pushed: list[str] = []
    skipped: list[str] = []
    for item in targets:
        pid = str(item.get("id") or "")
        name = str(item.get("name") or pid)
        try:
            blob, _fn = packs.export_zip(pid)
            r = httpx.post(
                f"{hub}/v1/packs",
                params={"name": name, "origin": dev, "id": pid},
                content=blob,
                headers={**_headers(dev, token), "Content-Type": "application/zip"},
                timeout=max(timeout, 180.0),
            )
            if r.status_code == 409:
                skipped.append(f"{name}（服务器上已有同 id 的包）")
                continue
            if r.status_code != 200:
                raise RuntimeError(_err_of(r))
            pushed.append(name)
        except Exception as exc:  # noqa: BLE001 —— 单个失败不拖垮其余
            skipped.append(f"{name}（{type(exc).__name__}: {exc}）")
            logger.warning("上传扩展包 %s 失败：%s", name, exc)

    if pushed:
        logger.info("上传扩展包到控制台 %s 个：%s", len(pushed), "、".join(pushed))
    return {"ok": True, "pushed": pushed, "skipped": skipped, "error": ""}


def _local_pack_exists(packs, pack_id: str) -> bool:
    """本机是否还有这个包（按 id 精确查，异常一律当"没有"）。"""
    try:
        for p in packs.list_packs():
            if str(p.get("id")) == str(pack_id):
                return True
    except Exception:  # noqa: BLE001 —— 查不到就重新拉，宁可多下一次
        return False
    return False


def _import_bytes(packs, blob: bytes, name: str) -> dict[str, Any]:
    """把下载到的 zip 字节导成本机包。

    复用 `packs.import_pack` 的 zip 分支：先落到临时文件再交给它，
    这样解包、manifest 解析、数据落位全部沿用已有逻辑，不另写一套。
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="zwpack-") as td:
        if not zipfile.is_zipfile(io.BytesIO(blob)):
            raise RuntimeError("下载到的不是合法 zip")
        zp = Path(td) / "pack.zip"
        zp.write_bytes(blob)
        return packs.import_pack(str(zp), name=name)
