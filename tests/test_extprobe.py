"""扩展探测（`extprobe`）的三态语义。

这一段直接影响用户对"插件到底有没有生效"的判断，所以语义必须钉死：

    True  = 实测能加载
    False = 实测不能加载（官方 Chrome 137+ 忽略 --load-extension）
    None  = **没测出来**（探测本身失败）

2026-11-19 云电脑真机踩到的 bug：探测抛 `PermissionError: [WinError 5] 拒绝访问`
（杀软拦了探针进程），旧代码返回 `False` —— 界面显示「不能加载扩展」，
用户以为插件坏了。实际是探测根本没跑成。
"""

from __future__ import annotations

import pytest


def _mod():
    return pytest.importorskip("zw_core.services.extprobe")


def test_probe_error_yields_unknown_not_false(monkeypatch, tmp_path):
    """探测抛异常时必须给 None（不知道），不能给 False（确定不行）。

    ## 为什么打在 `zw_core.services.proc` 上

    `extprobe` 是在**函数内** `from .proc import popen_hidden` 的，
    所以给 `extprobe.popen_hidden` 打补丁**没有用** —— 函数执行时又会从
    `.proc` 重新取。第一版就这么写的，CI（Windows）上真去执行了那个假 exe，
    抛的是 `OSError [WinError 216]（不是有效的 Win32 应用）`，
    而我当时断言"必须是 PermissionError"，于是红在断言上而不是红在行为上。

    教训：断言要看**行为**（返回 None、不缓存），别钉死具体异常类型 ——
    不同平台上同一个"起不来"抛的异常本来就不同。
    """
    ep = _mod()
    from zw_core.services import proc as proc_mod

    fake_bin = tmp_path / "chrome.exe"
    fake_bin.write_bytes(b"stub")

    def boom(*a, **k):
        raise PermissionError("[WinError 5] 拒绝访问。")

    monkeypatch.setattr(proc_mod, "popen_hidden", boom)
    monkeypatch.setattr(ep, "_save_cache", lambda d: None)

    out = ep.probe(fake_bin, use_cache=False)
    assert out["loadable"] is None, out
    assert "探测没能完成" in out["reason"], out["reason"]
    # 不钉死异常类型，只要把原因带出来（用户/我们排错时要看这个）
    assert "PermissionError" in out["reason"] or "OSError" in out["reason"], out["reason"]


def test_probe_error_is_not_cached(monkeypatch, tmp_path):
    """失败结果不能写缓存 —— 否则之后永远显示"不能加载"，再也不会重试。"""
    ep = _mod()
    fake_bin = tmp_path / "chrome.exe"
    fake_bin.write_bytes(b"stub")

    saved: list = []
    monkeypatch.setattr(ep, "_save_cache", lambda d: saved.append(dict(d)))
    from zw_core.services import proc as proc_mod

    monkeypatch.setattr(proc_mod, "popen_hidden",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    ep.probe(fake_bin, use_cache=False)
    assert not saved, f"探测失败却写了缓存：{saved}"


def test_peek_returns_none_when_no_cache(tmp_path):
    """`peek()` 只读缓存，没有就 None（不会去启动浏览器）。"""
    ep = _mod()
    b = tmp_path / "chrome.exe"
    b.write_bytes(b"stub")
    assert ep.peek(b) is None


def test_peek_ignores_cache_without_loadable(tmp_path, monkeypatch):
    """缓存里没有 `loadable` 字段的脏数据，`peek()` 要当成没有。"""
    ep = _mod()
    b = tmp_path / "chrome.exe"
    b.write_bytes(b"stub")
    monkeypatch.setattr(ep, "_load_cache", lambda: {ep._cache_key(b): {"reason": "x"}})
    assert ep.peek(b) is None
