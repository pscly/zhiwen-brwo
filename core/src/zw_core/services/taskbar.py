"""Windows 浏览器任务栏徽标。只处理已登记浏览器进程的可见主窗口。"""
from __future__ import annotations

import ctypes as C
import hashlib
import logging
import re
import sqlite3
import time
import unicodedata
from ctypes import wintypes as W
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable

log = logging.getLogger("zw.taskbar")
_PALETTE = ("b9410b", "2453a1", "127052", "7c3c94", "a52a48")
_WM_GETICON = 0x7F
_WM_SETICON = 0x80


def badge_for(name: str, profile_id: str) -> tuple[str, str]:
    """短名保留原样，长名使用名字开头的两个汉字或三个字母数字。"""
    digest = hashlib.sha256(profile_id.encode()).digest()
    normalized = unicodedata.normalize("NFKC", name).strip()
    chars = [c for c in normalized
             if (c.isascii() and c.isalnum()) or "\u4e00" <= c <= "\u9fff"]
    if not chars:
        chars = list(digest.hex()[:3].upper())
    limit = 3 if chars[0].isascii() else 4
    short: list[str] = []
    width = 0
    for char in chars:
        width += 1 if char.isascii() else 2
        if width > limit:
            break
        short.append(char)
    label = "".join(short)
    if len(short) < len(chars) and (number := re.search(r"[0-9]+$", normalized)):
        suffix = number.group()[-3:]
        if len(suffix) < 3 and chars[0].isascii() and chars[0].isalpha():
            suffix = chars[0] + suffix
        label = suffix
    return label, _PALETTE[digest[0] % len(_PALETTE)]


def _profile_badges(profiles: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """从所有环境分配短名，关闭邻近窗口时不重新编号。"""
    by_id = {p["id"]: badge_for(p["name"], p["id"]) for p in profiles}
    reserved = {label.casefold() for label, _ in by_id.values()}
    assigned: set[str] = set()
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    capacity = len(alphabet) ** 3
    for profile_id, (label, color) in sorted(by_id.items()):
        folded = label.casefold()
        if folded in assigned:
            seed = int.from_bytes(hashlib.sha256(profile_id.encode()).digest()[:4], "big")
            for offset in range(capacity):
                value = (seed + offset) % capacity
                candidate = "".join(alphabet[value // len(alphabet) ** i % len(alphabet)]
                                    for i in (2, 1, 0))
                folded = candidate.casefold()
                if folded not in reserved and folded not in assigned:
                    by_id[profile_id] = candidate, color
                    break
        assigned.add(folded)
    return by_id


class _BitmapHeader(C.Structure):
    _fields_ = [
        ("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
        ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
        ("image_size", W.DWORD), ("x", W.LONG), ("y", W.LONG),
        ("colors", W.DWORD), ("important", W.DWORD),
    ]


class _IconInfo(C.Structure):
    _fields_ = [("icon", W.BOOL), ("x", W.DWORD), ("y", W.DWORD),
                ("mask", W.HBITMAP), ("color", W.HBITMAP)]


class _Windows:
    def __init__(self) -> None:
        user, gdi, shell, kernel = (C.WinDLL(name, use_last_error=True)
                                    for name in ("user32", "gdi32", "shell32", "kernel32"))
        handle = W.HANDLE
        uint, integer, pointer = W.UINT, C.c_int, C.c_void_p
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

        def bind(dll: Any, name: str, ret: Any, *args: Any) -> Any:
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = ret, args
            return fn

        self.enum = bind(user, "EnumWindows", W.BOOL, self.callback, W.LPARAM)
        self.visible = bind(user, "IsWindowVisible", W.BOOL, W.HWND)
        self.owner = bind(user, "GetWindow", W.HWND, W.HWND, uint)
        self.window_class = bind(user, "GetClassNameW", integer, W.HWND, W.LPWSTR, integer)
        self.pid_for = bind(user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
        self.send = bind(user, "SendMessageTimeoutW", C.c_ssize_t,
                         W.HWND, uint, W.WPARAM, W.LPARAM, uint, uint, C.POINTER(C.c_size_t))
        self.draw_icon = bind(user, "DrawIconEx", W.BOOL, W.HDC, integer, integer, handle,
                              integer, integer, uint, handle, uint)
        self.draw_text = bind(user, "DrawTextW", integer, W.HDC, W.LPCWSTR, integer,
                              C.POINTER(W.RECT), uint)
        self.create_icon = bind(user, "CreateIconIndirect", handle, C.POINTER(_IconInfo))
        self.destroy_icon = bind(user, "DestroyIcon", W.BOOL, handle)
        self.extract = bind(shell, "ExtractIconExW", uint, W.LPCWSTR, integer,
                            C.POINTER(handle), C.POINTER(handle), uint)
        self.open_process = bind(kernel, "OpenProcess", handle, W.DWORD, W.BOOL, W.DWORD)
        self.process_image = bind(kernel, "QueryFullProcessImageNameW", W.BOOL,
                                  handle, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
        self.close_handle = bind(kernel, "CloseHandle", W.BOOL, handle)
        self.dc = bind(gdi, "CreateCompatibleDC", W.HDC, W.HDC)
        self.dib = bind(gdi, "CreateDIBSection", W.HBITMAP, W.HDC, C.POINTER(_BitmapHeader),
                        uint, C.POINTER(pointer), handle, W.DWORD)
        self.bitmap = bind(gdi, "CreateBitmap", W.HBITMAP, integer, integer, uint, uint, pointer)
        self.select = bind(gdi, "SelectObject", handle, W.HDC, handle)
        self.font = bind(gdi, "CreateFontW", handle, *([integer] * 5), *([W.DWORD] * 8), W.LPCWSTR)
        self.text_color = bind(gdi, "SetTextColor", W.DWORD, W.HDC, W.DWORD)
        self.bk = bind(gdi, "SetBkMode", integer, W.HDC, integer)
        self.flush = bind(gdi, "GdiFlush", W.BOOL)
        self.delete = bind(gdi, "DeleteObject", W.BOOL, handle)
        self.delete_dc = bind(gdi, "DeleteDC", W.BOOL, W.HDC)

    def message(self, hwnd: int, msg: int, slot: int, icon: int = 0) -> int | None:
        result = C.c_size_t()
        if self.send(hwnd, msg, slot, icon, 2, 200, C.byref(result)):
            return result.value
        return None

    def pid(self, hwnd: int) -> int:
        pid = W.DWORD()
        self.pid_for(hwnd, C.byref(pid))
        return pid.value

    def image_path(self, pid: int) -> str:
        process = self.open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ""
        try:
            path = C.create_unicode_buffer(32768)
            length = W.DWORD(len(path))
            if self.process_image(process, 0, path, C.byref(length)):
                return path.value
            return ""
        finally:
            self.close_handle(process)

    def windows(self, pids: set[int]) -> list[tuple[int, int]]:
        found = []

        @self.callback
        def visit(hwnd: int, _data: int) -> bool:
            pid = self.pid(hwnd)
            if pid in pids and self.visible(hwnd) and not self.owner(hwnd, 4):
                cls = C.create_unicode_buffer(64)
                self.window_class(hwnd, cls, len(cls))
                if cls.value == "Chrome_WidgetWin_1":
                    found.append((hwnd, pid))
            return True

        if not self.enum(visit, 0):
            raise C.WinError(C.get_last_error())
        return found

    def render(self, exe: str, text: str, color: str, n: int) -> int:
        base = W.HANDLE()
        if self.extract(exe, 0, C.byref(base), None, 1) != 1 or not base.value:
            raise OSError("读取浏览器图标失败")
        dc = self.dc(None)
        bmp = mask = font = previous = previous_font = None
        raw = C.c_void_p()
        try:
            if not dc:
                raise OSError("创建图标绘制上下文失败")
            bmp = self.dib(dc, C.byref(_BitmapHeader(40, n, -n, 1, 32)), 0,
                           C.byref(raw), None, 0)
            empty_mask = C.create_string_buffer(((n + 15) // 16) * 2 * n)
            mask = self.bitmap(n, n, 1, 1, empty_mask)
            if not bmp or not mask or not raw.value:
                raise OSError("创建图标位图失败")
            selected = self.select(dc, bmp)
            if selected in (None, 0, C.c_void_p(-1).value):
                raise OSError("选择图标位图失败")
            previous = selected
            C.memset(raw, 0, 4 * n * n)
            if not self.draw_icon(dc, 0, 0, base, n, n, 0, None, 3):
                raise OSError("绘制浏览器底图失败")
            self.flush()
            # 下方的高对比色带是图标本身的一部分，小任务栏图标也能显示。
            left, right, top, bottom = 1, n - 1, n * 5 // 8, n - 1
            rgb = bytes.fromhex(color)
            band = bytes((*reversed(rgb), 255))
            pix = (C.c_ubyte * (4 * n * n)).from_address(raw.value)
            for y in range(top, bottom):
                inset = 2 if y in (top, bottom - 1) else 0
                a, b = left + inset, right - inset
                pix[4 * (y * n + a):4 * (y * n + b)] = band * (b - a)
            font = self.font(-int(n * .30), 0, 0, 0, 700, 0, 0, 0,
                             1, 0, 0, 4, 0, "Microsoft YaHei UI" if not text.isascii() else "Segoe UI")
            if not font:
                raise OSError("创建徽标字体失败")
            selected = self.select(dc, font)
            if selected in (None, 0, C.c_void_p(-1).value):
                raise OSError("选择徽标字体失败")
            previous_font = selected
            self.bk(dc, 1)
            self.text_color(dc, 0xFFFFFF)
            if not self.draw_text(dc, text, -1, C.byref(W.RECT(left, top, right, bottom)), 0x825):
                raise OSError("绘制徽标文字失败")
            self.flush()
            # GDI 写 RGB 时会清掉目标像素的 alpha。
            for y in range(top, bottom):
                inset = 2 if y in (top, bottom - 1) else 0
                for x in range(left + inset, right - inset):
                    pix[4 * (y * n + x) + 3] = 255
            icon = self.create_icon(C.byref(_IconInfo(True, 0, 0, mask, bmp)))
            if not icon:
                raise OSError("创建带徽标的窗口图标失败")
            return icon
        finally:
            if previous_font:
                self.select(dc, previous_font)
            if previous:
                self.select(dc, previous)
            for obj in (font, bmp, mask):
                if obj:
                    self.delete(obj)
            if dc:
                self.delete_dc(dc)
            self.destroy_icon(base)


@dataclass
class _Applied:
    pid: int
    key: tuple[int, str, str, str]
    icons: tuple[int, int]
    old: list[int | None]


class _Badges:
    def __init__(self) -> None:
        self.win = _Windows()
        self.applied: dict[int, _Applied] = {}
        self.icons: dict[tuple[int, str, str, str], tuple[int, int]] = {}
        self.failed_badges: set[tuple[int, str, str, str]] = set()

    def refresh(self, hwnd: int, item: _Applied) -> None:
        for slot, handle in enumerate(item.icons):
            if self.win.pid(hwnd) != item.pid:
                return
            current = self.win.message(hwnd, _WM_GETICON, slot)
            if current is None or current == handle:
                continue
            # Keep the latest Chromium icon. A timed-out SET can still have run.
            item.old[slot] = current
            previous = self.win.message(hwnd, _WM_SETICON, slot, handle)
            if previous is not None and previous != handle:
                item.old[slot] = previous

    def restore(self, hwnd: int, item: _Applied, deadline: float = float("inf")) -> bool:
        restored = True
        for slot, (handle, old) in enumerate(zip(item.icons, item.old, strict=True)):
            if self.win.pid(hwnd) != item.pid:
                return True
            if old is None:
                continue
            if time.monotonic() >= deadline:
                return False
            current = self.win.message(hwnd, _WM_GETICON, slot)
            if current is None:
                restored = False
            elif current == handle:
                if time.monotonic() >= deadline:
                    return False
                if self.win.message(hwnd, _WM_SETICON, slot, old) is None:
                    restored = False
        return restored

    def _render_pair(self, exe: str, label: str, color: str) -> tuple[int, int]:
        small = self.win.render(exe, label, color, 32)
        try:
            return small, self.win.render(exe, label, color, 48)
        except OSError:
            self.win.destroy_icon(small)
            raise

    def update(self, profiles: list[dict[str, Any]], stop: Event | None = None) -> None:
        labels = _profile_badges(profiles)
        wanted = {int(p["pid"]): labels[p["id"]] for p in profiles
                  if p.get("pid") and p.get("state") == "running" and not p.get("headless")}
        windows = dict(self.win.windows(set(wanted))) if wanted else {}
        for hwnd, item in list(self.applied.items()):
            if stop is not None and stop.is_set():
                return
            pid = windows.get(hwnd)
            if pid is not None and item.pid == pid and item.key[2:] == wanted[pid]:
                self.refresh(hwnd, item)
            elif self.restore(hwnd, item):
                del self.applied[hwnd]
        failed: set[tuple[int, str, str, str]] = set()
        for hwnd, pid in windows.items():
            if stop is not None and stop.is_set():
                return
            if hwnd in self.applied or self.win.pid(hwnd) != pid:
                continue
            exe = self.win.image_path(pid)
            if not exe:
                continue
            key = (pid, exe, *wanted[pid])
            if key in failed:
                continue
            if key not in self.icons:
                try:
                    self.icons[key] = self._render_pair(*key[1:])
                except OSError as exc:
                    if key not in self.failed_badges:
                        log.warning("浏览器任务栏徽标绘制失败：%s", exc)
                    failed.add(key)
                    continue
            item = _Applied(pid, key, self.icons[key], [None, None])
            self.applied[hwnd] = item
            self.refresh(hwnd, item)
        self.failed_badges = failed
        self._release_unused()

    def close(self) -> None:
        deadline = time.monotonic() + 1.0
        for _ in range(2):
            for hwnd, item in list(self.applied.items()):
                if time.monotonic() >= deadline:
                    break
                if self.restore(hwnd, item, deadline):
                    del self.applied[hwnd]
        self._release_unused()
        if self.applied:
            log.warning("退出时有 %d 个窗口仍持有徽标图标，保留句柄至进程退出", len(self.applied))

    def _release_unused(self) -> None:
        used = {item.key for item in self.applied.values()}
        for key in set(self.icons) - used:
            for icon in self.icons.pop(key):
                self.win.destroy_icon(icon)


def native_selftest(system_icon: str) -> str:
    """验证打包后的 Win32 绘制和句柄释放，不打开浏览器。"""
    win = _Windows()
    for text, size in (("c19", 32), ("甲乙", 48)):
        icon = win.render(system_icon, text, _PALETTE[0], size)
        if not win.destroy_icon(icon):
            raise OSError("释放自检图标失败")
    return "32 和 48 像素图标绘制成功"


def watch(stop: Event, profiles: Callable[[], list[dict[str, Any]]]) -> None:
    try:
        badges = _Badges()
    except (OSError, AttributeError) as exc:
        log.warning("任务栏徽标初始化失败：%s", exc)
        return
    reported = False
    try:
        while not stop.is_set():
            try:
                badges.update(profiles(), stop)
                reported = False
            except (OSError, sqlite3.Error) as exc:
                if not reported:
                    log.warning("任务栏徽标更新失败：%s", exc)
                    reported = True
            stop.wait(2.0)
    finally:
        badges.close()
