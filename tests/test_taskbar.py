"""任务栏短名和图标生命周期回归。"""
from zw_core.services import taskbar


def test_badge_shortens_long_names_and_preserves_short_names():
    assert taskbar.badge_for("c19", "1")[0] == "c19"
    assert taskbar.badge_for("甲乙丙丁", "2")[0] == "甲乙"
    assert taskbar.badge_for("  Ｃ－１９  ", "1")[0] == "C19"
    assert taskbar.badge_for("🎈", "1") == taskbar.badge_for("🎈", "1")
    assert len(taskbar.badge_for("🎈", "1")[0]) == 3
    assert taskbar.badge_for("业务环境01", "1")[0] == "01"
    assert taskbar.badge_for("业务环境02", "2")[0] == "02"


def test_same_prefix_badges_are_unique_and_stable():
    profiles = [{"id": "profile-1", "name": "窗口甲", "state": "running"},
                {"id": "profile-5", "name": "窗口戊", "state": "running"}]
    badges = taskbar._profile_badges(profiles)
    assert badges["profile-1"][0] != badges["profile-5"][0]
    assert taskbar._profile_badges([dict(p, state="stopped") for p in reversed(profiles)]) == badges


def test_badges_follow_profile_rename_and_restore_original_icons(monkeypatch):
    class Windows:
        def __init__(self):
            self.handles = {0: 41, 1: 42}
            self.destroyed = []
            self.serial = 100
            self.timeout_once = True

        def pid(self, hwnd):
            return 21 if hwnd == 7 else 0

        def image_path(self, pid):
            return "chrome.exe" if pid == 21 else ""

        def windows(self, pids):
            return [(7, 21)] if 21 in pids else []

        def render(self, _exe, _label, _color, _size):
            self.serial += 1
            return self.serial

        def message(self, _hwnd, msg, slot, icon=0):
            old = self.handles[slot]
            if msg == 0x80:
                if slot == 1 and self.timeout_once:
                    self.timeout_once = False
                    return None
                self.handles[slot] = icon
            return old

        def destroy_icon(self, icon):
            assert icon not in self.handles.values()
            self.destroyed.append(icon)

    win = Windows()
    monkeypatch.setattr(taskbar, "_Windows", lambda: win)
    manager = taskbar._Badges()
    profile = {"id": "test", "name": "c19", "pid": 21, "engine_path": "", "state": "running"}
    manager.update([profile])
    assert list(win.handles.values()) == [101, 42]
    manager.update([profile])
    assert list(win.handles.values()) == [101, 102]
    win.handles[1] = 43  # Chromium later chose a different native icon.
    manager.update([profile])
    assert list(win.handles.values()) == [101, 102]
    assert win.serial == 102
    manager.update([{**profile, "name": "甲乙"}])
    assert list(win.handles.values()) == [103, 104]
    assert sorted(win.destroyed) == [101, 102]
    manager.close()
    assert win.handles == {0: 41, 1: 43}
    assert sorted(win.destroyed) == [101, 102, 103, 104]
