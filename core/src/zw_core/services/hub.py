"""hub 客户端：把加密快照推到同步中心，或从中心拉回来。

全部请求都是普通 HTTP（局域网），**不依赖 HTTPS**：
- 机密性：包体本身是 AEAD 密文，hub 解不开；
- 完整性：包体自带认证标签，改一个字节就解不开；
- 可选加固：`ZW_HUB_SIGN=1` 时对请求做 HMAC 签名 + 时间戳防重放（hub 侧需同样开启）。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx


class HubError(Exception):
    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class HubClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 60.0,
        sign_secret: str = "",
    ) -> None:
        if not base_url:
            raise HubError("HUB_NOT_CONFIGURED", "没有配置同步中心地址（ZW_HUB_URL）")
        self.base = base_url.rstrip("/")
        self.token = token
        self.sign_secret = sign_secret
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    # ── 内部 ──────────────────────────────────────────────
    def _headers(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        h: dict[str, str] = {"User-Agent": "zhiwen-core/0.1"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.sign_secret:
            ts = str(int(time.time()))
            body_hash = hashlib.sha256(body).hexdigest()
            msg = f"{method}\n{path}\n{ts}\n{body_hash}".encode()
            h["X-ZW-Ts"] = ts
            h["X-ZW-Sign"] = hmac.new(self.sign_secret.encode(), msg, hashlib.sha256).hexdigest()
        return h

    def _call(self, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None) -> httpx.Response:
        h = self._headers(method, path, body)
        h.update(headers or {})
        try:
            resp = self._client.request(method, self.base + path, content=body, headers=h)
        except httpx.HTTPError as exc:
            raise HubError("HUB_UNREACHABLE", f"连不上同步中心 {self.base}：{exc}") from exc
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            raise HubError(
                payload.get("code") or f"HTTP_{resp.status_code}",
                payload.get("message") or resp.text[:300],
                status=resp.status_code,
            )
        return resp

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = b"" if body is None else __import__("json").dumps(body, ensure_ascii=False).encode()
        resp = self._call(method, path, raw, {"Content-Type": "application/json"} if body else None)
        data = resp.json()
        return data.get("data", data)

    def close(self) -> None:
        self._client.close()

    # ── 业务 ──────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        return self._json("GET", "/healthz")

    def meta(self) -> dict[str, Any]:
        return self._json("GET", "/v1/meta")

    def manifest(self) -> dict[str, Any]:
        """给 AI 看的自描述清单。"""
        return self._json("GET", "/v1/ai/manifest")

    def index(self) -> list[dict[str, Any]]:
        return self._json("GET", "/v1/index").get("profiles", [])

    def latest(self, profile_id: str) -> dict[str, Any] | None:
        try:
            return self._json("GET", f"/v1/latest/{profile_id}")
        except HubError as exc:
            if exc.code in ("NO_LATEST", "HTTP_404"):
                return None
            raise

    def list_revisions(self, profile_id: str) -> list[dict[str, Any]]:
        return self._json("GET", f"/v1/blobs/{profile_id}").get("revisions", [])

    def put_blob(
        self,
        profile_id: str,
        revision: int,
        blob: bytes,
        key_fp: str,
        source_node: str = "",
        note: str = "",
        cas: bool = True,
    ) -> dict[str, Any]:
        # HTTP 头只能是 ascii，备注里常有中文 —— 统一 URL 编码后再放进去
        from urllib.parse import quote

        headers = {
            "Content-Type": "application/octet-stream",
            "X-ZW-Key-Fp": key_fp,
            "X-ZW-Source-Node": quote(source_node, safe=""),
            "X-ZW-Note": quote(note, safe=""),
            "X-ZW-Encoded": "url",
        }
        if not cas:
            headers["X-ZW-No-Cas"] = "1"
        resp = self._call("PUT", f"/v1/blobs/{profile_id}/{revision}", blob, headers)
        return resp.json().get("data", {})

    def get_blob(self, profile_id: str, revision: int) -> tuple[bytes, dict[str, str]]:
        resp = self._call("GET", f"/v1/blobs/{profile_id}/{revision}")
        return resp.content, {
            "sha256": resp.headers.get("X-ZW-Sha256", ""),
            "key_fp": resp.headers.get("X-ZW-Key-Fp", ""),
        }

    def publish_latest(self, profile_id: str, revision: int, force: bool = False) -> dict[str, Any]:
        return self._json("PUT", f"/v1/latest/{profile_id}", {"revision": revision, "force": force})

    def acquire_lease(self, profile_id: str, owner: str, kind: str = "sync", ttl: int = 90) -> dict[str, Any]:
        return self._json("POST", f"/v1/leases/{profile_id}", {"owner": owner, "kind": kind, "ttl": ttl})

    def heartbeat_lease(self, profile_id: str, owner: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/leases/{profile_id}/heartbeat", {"owner": owner})

    def release_lease(self, profile_id: str, owner: str) -> None:
        try:
            self._call("DELETE", f"/v1/leases/{profile_id}?owner={owner}")
        except HubError:
            pass

    def get_lease(self, profile_id: str) -> dict[str, Any] | None:
        return self._json("GET", f"/v1/leases/{profile_id}")
