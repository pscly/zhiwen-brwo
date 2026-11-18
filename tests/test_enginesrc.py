"""内核获取（enginesrc）的测试。

覆盖三类最容易出错的地方：
1. **校验**：下载被截断/被替换必须中止，不能"先装上再说"
2. **解包安全**：压缩包里有越界路径必须拒绝
3. **策略**：`ZW_ENGINE_POLICY=ungoogled` 时**不静默退回官方 Chrome**

真实网络下载另外用 `zw engine install` 手工验过（见 CHANGELOG）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_spec_for_platform_picks_right_asset():
    from zw_core.services import enginesrc

    win = enginesrc.spec_for_platform("Windows", "x86_64")
    assert win and "ungoogled-chromium-windows" in win.repo
    assert win.matches("ungoogled-chromium_157.0.8103.78-1.1_windows_x64.zip")
    assert not win.matches("ungoogled-chromium_157_windows_arm64.zip")

    lin = enginesrc.spec_for_platform("Linux", "x86_64")
    assert lin and lin.kind == "tarxz"
    assert lin.matches("ungoogled-chromium-156.0.8078.79-1-x86_64_linux.tar.xz")

    # macOS 上游没有稳定二进制 → 明确返回 None（调用方给出可操作提示）
    assert enginesrc.spec_for_platform("Darwin", "arm64") is None


def test_install_archive_rejects_path_traversal(tmp_path):
    """压缩包里有 `../` 越界路径必须拒绝装。"""
    from zw_core.services import enginesrc

    evil = tmp_path / "evil.zip"
    evil.write_bytes(_make_zip({
        "ungoogled/x/chrome": b"#!/bin/sh\n",
        "../../../../tmp/pwned": b"pwn",
    }))
    with pytest.raises(enginesrc.EngineSourceError) as ei:
        enginesrc.install_archive(evil, kind="zip", binary="chrome",
                                  engine_dir=tmp_path / "engines", version_tag="v1")
    assert ei.value.code == "ENGINE_EXTRACT_FAILED"


def test_install_archive_reports_missing_binary(tmp_path):
    from zw_core.services import enginesrc

    z = tmp_path / "nobin.zip"
    z.write_bytes(_make_zip({"readme.txt": b"nothing here"}))
    with pytest.raises(enginesrc.EngineSourceError) as ei:
        enginesrc.install_archive(z, kind="zip", binary="chrome",
                                  engine_dir=tmp_path / "engines", version_tag="v1")
    assert ei.value.code == "ENGINE_EXTRACT_FAILED"


def test_download_checksum_mismatch_aborts_and_leaves_nothing(tmp_path, monkeypatch):
    """校验不通过必须丢弃，且不能留下半成品文件。"""
    from zw_core.services import enginesrc

    payload = b"x" * 1000
    good = hashlib.sha256(payload).hexdigest()

    class _Resp:
        status_code = 200
        headers = {"content-length": str(len(payload))}

        def iter_bytes(self, n=0):
            yield payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _C:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url):
            return _Resp()

    monkeypatch.setattr(enginesrc, "_client", lambda s, timeout=30.0: _C())

    dest = tmp_path / "eng.tar.xz"
    # 校验值故意给错
    with pytest.raises(enginesrc.EngineSourceError) as ei:
        enginesrc.download("http://x/y", dest, sha256="0" * 64, size=len(payload))
    assert ei.value.code == "ENGINE_CHECKSUM_MISMATCH"
    assert not dest.exists(), "校验失败却留下了文件"
    assert not list(tmp_path.glob("*.part")), "校验失败却留下了 .part"

    # 给对校验值就能成功
    enginesrc.download("http://x/y", dest, sha256=good, size=len(payload))
    assert dest.read_bytes() == payload


def test_resolve_latest_without_digest_refuses(monkeypatch, tmp_path):
    """资产没有 sha256 digest 时**拒绝安装**，不能闭眼装二进制。"""
    from zw_core.services import enginesrc

    class _R:
        status_code = 200

        def json(self):
            return {"tag_name": "1.0", "assets": [
                {"name": "x_windows_x64.zip", "browser_download_url": "http://x",
                 "size": 1, "digest": None}]}

    class _C:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _R()

    monkeypatch.setattr(enginesrc, "_client", lambda s, timeout=30.0: _C())
    spec = enginesrc.spec_for_platform("Windows", "x86_64")
    with pytest.raises(enginesrc.EngineSourceError) as ei:
        enginesrc.resolve_latest(None, spec=spec)
    assert ei.value.code == "ENGINE_CHECKSUM_MISSING"


def test_policy_ungoogled_does_not_fall_back_to_system_chrome(tmp_path, monkeypatch):
    """`policy=ungoogled` 时，即使系统装了官方 Chrome 也不能悄悄用。

    官方 Chrome 137+ 忽略 `--load-extension`，退回它等于悄悄丢掉插件能力。
    """
    from zw_core.services import engines as engsvc
    from zw_core.config import Settings

    s = Settings()
    s.root = tmp_path
    s.engine_dir = tmp_path / "engines"
    s.engine_policy = "ungoogled"
    s.chrome_bin = ""
    s.engine_dir.mkdir(parents=True)

    # 造一个假的官方 chrome 在系统位置：让 PATH 里能找到
    fake = tmp_path / "bin" / "chrome"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(engsvc, "_POSIX_CANDIDATES", (str(fake),))

    with pytest.raises(engsvc.EngineError) as ei:
        engsvc.find_chrome(s)
    assert ei.value.code in ("ENGINE_NOT_FOUND", "ENGINE_PREPARING"), ei.value.code
    assert "ungoogled" in str(ei.value).lower()


def test_policy_ungoogled_uses_engine_dir_ungoogled_build(tmp_path, monkeypatch):
    """引擎目录里有 ungoogled 时优先用它（而不是同目录里的官方 chrome）。"""
    from zw_core.services import engines as engsvc
    from zw_core.config import Settings

    s = Settings()
    s.root = tmp_path
    s.engine_dir = tmp_path / "engines"
    s.engine_policy = "ungoogled"
    s.chrome_bin = ""

    good = s.engine_dir / "ungoogled-chromium-156" / "chrome"
    other = s.engine_dir / "google-chrome-157" / "chrome"
    for p in (good, other):
        p.parent.mkdir(parents=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)

    got = engsvc.find_chrome(s)
    assert "ungoogled" in str(got).lower(), got


def test_ensure_full_pipeline_over_real_http(tmp_path, monkeypatch):
    """走完真实链路：HTTP 下载 → sha256 校验 → 解包 → 落盘 → `--version` 自检。

    上轮只做到"用本机已有的内核验证扩展能加载"，联网下载那段因为 GitHub 限速
    没跑完。这里起一个本地 HTTP 服务器，用**真实**的压缩包和**真实**的校验值
    把 `ensure()` 整条路径跑一遍（不是 mock 掉下载，只有镜像地址换成 127.0.0.1）。

    另外顺带验幂等：第二次调用必须跳过，不能重复下载。
    """
    import hashlib
    import http.server
    import io
    import os
    import stat
    import threading
    import zipfile

    from zw_core import config
    from zw_core.services import enginesrc

    # 造一个"最小可用内核"：真的能跑 `--version`
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("fake-chromium/chrome",
                    "#!/bin/sh\necho 'Chromium 999.0.1.2'\n")
    blob = payload.getvalue()
    served = tmp_path / "eng.zip"
    served.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()

    class _H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
        monkeypatch.setenv("ZW_ENGINE_URL", f"http://127.0.0.1:{port}/eng.zip")
        monkeypatch.setenv("ZW_ENGINE_SHA256", sha)
        monkeypatch.setenv("ZW_ENGINE_VERSION_TAG", "fake-999")
        monkeypatch.setenv("ZW_DOWNLOAD_PROXY", "")   # 别让本机代理配置影响
        config.reset_settings()

        res = enginesrc.ensure(force=False, s=config.get_settings())
        assert res["ok"] is True, res
        assert res["skipped"] is False, res
        assert "999" in str(res.get("version")), res

        binp = Path(res["path"])
        assert binp.is_file(), res
        assert "fake-999" in str(binp), res
        # 可执行位必须被补上（否则解包出来的脚本跑不了）
        assert os.access(binp, os.X_OK), "解包后没有可执行权限"
        assert binp.stat().st_mode & stat.S_IXUSR

        prog = enginesrc.progress()
        assert prog["state"] == "done", prog
        assert prog["bytes_done"] == len(blob), prog

        # 幂等：已有内核时第二次必须跳过
        again = enginesrc.ensure(force=False, s=config.get_settings())
        assert again["skipped"] is True, again
    finally:
        httpd.shutdown()
        monkeypatch.delenv("ZW_ENGINE_URL", raising=False)
        monkeypatch.delenv("ZW_ENGINE_SHA256", raising=False)
        monkeypatch.delenv("ZW_ENGINE_VERSION_TAG", raising=False)
        monkeypatch.delenv("ZW_DOWNLOAD_PROXY", raising=False)
        config.reset_settings()
