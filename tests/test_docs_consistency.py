"""文档与代码的一致性。

这个仓库的规矩是"加错误码必须同步更新 docs/04-接口文档.md §10"，
人工守约迟早会漏。这里把它变成一条会失败的测试。

同类问题还有：AI 自描述清单里的端点表 —— 加了路由却忘了写进清单，
AI 就发现不了那个能力。也一并检查。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _raised_codes() -> set[str]:
    codes: set[str] = set()
    for f in list((ROOT / "core/src").rglob("*.py")) + list((ROOT / "hub/src").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(
            r'(?:ApiError|ProfileError|SyncError|HubError|BundleError|EngineError'
            r'|ProxyError|CDPError|CryptoError)\(\s*(?:\d+\s*,\s*)?"([A-Z][A-Z0-9_]{2,})"',
            text,
        ):
            codes.add(m.group(1))
    return codes


def _documented_codes() -> set[str]:
    doc = (ROOT / "docs/04-接口文档.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]{2,})`", doc, re.M))


def test_every_raised_error_code_is_documented():
    missing = sorted(_raised_codes() - _documented_codes())
    assert not missing, (
        "这些错误码在源码里会返回，但 docs/04-接口文档.md 的错误码表里没有：\n  "
        + "\n  ".join(missing)
        + "\n（加错误码时请同步补文档 §10）"
    )


def test_no_doc_only_error_codes_that_look_like_typos():
    """文档里有、代码里没有的码，只允许是这几个"框架层"抛出、不在业务代码里的。

    其余的要么是历史残留，要么是拼写不一致 —— 都该清掉或补代码。
    """
    allowed = {"INTERNAL", "NOT_READY", "STORAGE_READONLY", "UNAUTHENTICATED", "CIDR_FORBIDDEN"}
    extra = _documented_codes() - _raised_codes() - allowed
    assert not extra, f"文档里记了但代码不会返回的错误码（拼接错误或残留）：{sorted(extra)}"


def test_ai_manifest_lists_the_core_endpoints():
    """AI 靠 /ai/manifest 的端点清单发现能力，漏了就等于那个能力不存在。"""
    from zw_core.api import build_app

    app = build_app()
    manifest_fn = None
    for route in app.routes:
        if getattr(route, "path", "") == "/api/v1/ai/manifest":
            manifest_fn = route.endpoint
            break
    assert manifest_fn is not None
    manifest = manifest_fn()
    # 清单里的路径是 f-string 渲染过的（{{id}} → {id}），标签统一成 {id} 便于比较
    listed = {e["path"].replace("{ref}", "{id}") for e in manifest["data"]["端点清单"]}

    # 必须出现在清单里的关键能力（不是全部路由 —— 参数化路径的写法与清单不同）
    required = [
        "/api/v1/profiles",
        "/api/v1/profiles/{id}/start",
        "/api/v1/profiles/{id}/cookies",
        "/api/v1/profiles/{id}/fingerprint/probe",
        "/api/v1/profiles/{id}/export",
        "/api/v1/sync/{id}/push",
        "/api/v1/sync/{id}/pull",
        "/api/v1/proxies",
        "/api/v1/extensions",
        "/api/v1/groups",
        "/cdp/{id}/json/version",
    ]
    missing = [p for p in required if p not in listed]
    assert not missing, f"AI 自描述清单缺少这些端点，AI 会发现不到：{missing}"


def test_ui_has_no_duplicate_ids():
    """控制台是单文件手写 DOM，重复 id 会让 querySelector 命中错元素。

    真的踩过：工具栏的分组筛选和表单里的分组输入框都叫 f-group。
    """
    import collections

    html = (ROOT / "core/src/zw_core/web/index.html").read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([a-zA-Z0-9_\-]+)"', html)
    dups = {k: v for k, v in collections.Counter(ids).items() if v > 1}
    assert not dups, f"控制台里有重复 id：{dups}"


def test_ui_has_no_external_resources():
    """内网离线环境：控制台不能引用任何外部 CDN / 字体 / 脚本。"""
    html = (ROOT / "core/src/zw_core/web/index.html").read_text(encoding="utf-8")
    for bad in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "googleapis", "cdnjs"):
        assert bad not in html, f"控制台引用了外部资源 {bad}，在离线内网里会白屏"


def test_ui_does_not_use_native_dialogs():
    """原生 alert/confirm/prompt 的样式不可控，且在自动化里会阻塞。"""
    html = (ROOT / "core/src/zw_core/web/index.html").read_text(encoding="utf-8")
    # 去掉三种注释再查，避免把说明文字当成调用：
    # HTML 注释（<!-- -->）、JS 块注释、JS 行注释。
    # 这条断言真的抓到过一次"注释里写了不用 prompt 但代码里还留着 prompt("的漏改。
    code = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for fn in ("window.alert(", "alert(", "confirm(", "window.prompt(", "prompt("):
        hits = [m for m in re.finditer(re.escape(fn), code)]
        # 允许 askText/confirmDanger 这类自定义实现里的同名片段，检查调用形式
        real = [h for h in hits if "function " not in code[max(0, h.start() - 30):h.start()]]
        assert not real, f"控制台用了原生 {fn}，应改自定义弹窗（见 askText / confirmDanger）"
