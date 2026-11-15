# PyInstaller 打包配置：把客户端打成一个可双击的程序。
#
# 用法（各平台各自构建，不能交叉编译）：
#   pyinstaller packaging/zhiwen-client.spec --noconfirm
#
# 产物：
#   Windows  dist/知纹客户端.exe      （--windowed，双击不弹黑框）
#   Linux    dist/zhiwen-client       （单文件，配合 AppImage 使用）
#
# 为什么要把 zw_core 整个包进来：客户端要在本机跑 core（浏览器只能在本地开）。
# 打包后不能再靠"找系统 python 再起子进程"，所以 core 是**进程内线程**跑的。

import sys
from pathlib import Path

# spec 文件执行时 __file__ 不可用，用 SPECPATH（PyInstaller 注入）
ROOT = Path(SPECPATH).resolve().parent          # noqa: F821
SRC = ROOT / "core" / "src"

# 控制台页面必须显式带上：它是运行时读文件的，PyInstaller 的依赖分析看不到
datas = [(str(SRC / "zw_core" / "web"), "zw_core/web")]

# tkinter 的 Tcl/Tk 运行库与脚本目录必须显式收进来。
# 不写这段的话，产物在"构建机上能跑、换台机器就报 libtcl9.0.so not found"——
# 因为它默认只收了 python 的 _tkinter 扩展，没收 Tcl 本体。
# Windows/mac 上 PyInstaller 有现成 hook 会自动处理，Linux 上不行。
if sys.platform.startswith("linux"):
    import glob as _glob
    import os as _os
    import sysconfig as _sysconfig

    # 搜索位置要含**构建用的那个 Python 自己的目录**：
    # uv / pyenv 装的独立 CPython 会把 libtcl9.0.so 放在
    # ~/.local/share/uv/python/cpython-X.Y.Z/lib/ 下，只扫 /usr/lib 会漏掉，
    # 产物就变成"构建机上看着正常，一跑就说 libtcl9.0.so not found"。
    bases = ["/usr/lib", "/usr/lib/x86_64-linux-gnu", "/usr/local/lib"]
    for key in ("LIBDIR", "LIBPL", "prefix", "exec_prefix"):
        v = _sysconfig.get_config_var(key) or _sysconfig.get_config_var(key.upper())
        if v and _os.path.isdir(v):
            bases.append(v)
    # Python 自身目录（uv 布局在这里放 tcl/tk）
    bases.append(_os.path.dirname(_os.path.dirname(_sysconfig.get_paths()["stdlib"])))
    seen_files, seen_dirs = set(), set()
    for base in bases:
        for pat in ("libtcl*.so*", "libtk*.so*"):
            for f in _glob.glob(_os.path.join(base, pat)):
                if f in seen_files:
                    continue
                seen_files.add(f)
                datas.append((f, "."))
        for d in ("tcl8.6", "tk8.6", "tcl9.0", "tk9.0"):
            p = _os.path.join(base, d)
            if _os.path.isdir(p) and p not in seen_dirs:
                seen_dirs.add(p)
                datas.append((p, d))
    # 独立的 tcl9tk9.0.so 与 thread/itcl 子库也要收，否则 tcl 初始化会找 tcl_library
    for pat in ("libtcl*tk*.so*",):
        for base in bases:
            for f in _glob.glob(_os.path.join(base, pat)):
                if f in seen_files:
                    continue
                seen_files.add(f)
                datas.append((f, "."))

hiddenimports = [
    # uvicorn 的这些后端是运行时按名字加载的，静态分析找不到
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # websockets 的 asyncio 实现同上
    "websockets.legacy",
    "websockets.legacy.client",
    # HTTP 客户端
    "httpx",
    "httpcore",
    # 加密与压缩
    "nacl",
    "nacl.bindings",
    "argon2",
    "argon2.low_level",
    "zstandard",
    # GUI
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    # 打包后 core 的 web 目录解析
    "zw_core.web",
]

a = Analysis(
    [str(SRC / "zw_core" / "client.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 明确排除掉用不到的大件，减小体积
    excludes=["pytest", "pandas", "numpy", "matplotlib", "PyQt5", "PySide2", "test"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

name = "知纹客户端" if sys.platform == "win32" else "zhiwen-client"

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压缩容易被杀软误报，这里关掉
    console=False,       # 关键：双击不弹黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# macOS 额外打一个 .app 包（双击能开；未签名，首次要右键「打开」）。
# 单文件 EXE 在上面已经产出了，这里只是多给一个更像安装包的形式。
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        exe,
        name="知纹客户端.app",
        icon=None,
        bundle_identifier="cc.pscly.zhiwen.client",
        info_plist={
            "CFBundleName": "知纹客户端",
            "CFBundleDisplayName": "知纹客户端",
            "CFBundleShortVersionString": "0.3.0",
            "NSHighResolutionCapable": True,
            # 客户端要联网到自己填的服务器地址，不需要沙箱
            "LSMinimumSystemVersion": "11.0",
        },
    )
