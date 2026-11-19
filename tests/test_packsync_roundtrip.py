"""整条链：hub 存包 → core 拉包 → 本机包里**数据和代码都在**。

## 这条测试守的是什么

用户要求「把插件及其数据打包放到控制台，以后建环境默认带上」。
拆开是四步，任何一步断了整件事就不成立：

```
core 导出 zip  →  上传到 hub  →  core 下载  →  导入本机包（带数据）
                              ↑
                      这一段 0.8.14 才补上
```

这里用**同一个进程**跑完 hub 侧模块和 core 侧模块，避免起真服务器，
但走的是真实代码路径（put → read_bytes → import_pack），不是 mock。
"""

from __future__ import annotations

import io
import json
import zipfile


def _pack_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("code/extkey/manifest.json",
                   json.dumps({"name": "跨机扩展", "version": "2.0"}))
        z.writestr("code/extkey/sw.js", "void 0;")
        z.writestr("data/extkey/000003.log", b"LOGIN-STATE")
    return buf.getvalue()


def test_hub_pack_imports_locally_with_data(tmp_path, monkeypatch):
    from zw_hub import packstore
    from zw_hub.config import Settings
    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "core"))
    config.reset_settings()

    hub_dir = tmp_path / "hub"
    hub_dir.mkdir(parents=True, exist_ok=True)
    hs = Settings()
    hs.data_dir = hub_dir
    packstore.ensure_schema(hs)

    try:
        # ① hub 存包
        info = packstore.put(hs, _pack_zip(), name="跨机包", origin="dev")
        assert info["extension_count"] == 1
        assert info["has_data"] is True

        # ② core 下载（真走 read_bytes，含 sha 校验）
        blob, sha = packstore.read_bytes(hs, info["id"])
        assert sha == info["sha256"]

        # ③ core 导入（用 downloads 得到的字节，走和 packsync 一样的路径）
        zp = tmp_path / "downloaded.zip"
        zp.write_bytes(blob)
        local = packs.import_pack(str(zp), name="跨机包")
        assert local["extension_count"] == 1, local
        ext = local["extensions"][0]
        assert ext["has_data"] is True, f"跨机搬过来数据丢了：{ext}"

        # ④ 数据文件真的在了
        pdir = packs.packs_root() / local["id"]
        got = pdir / str(ext["data_dir"]) / "000003.log"
        assert got.is_file() and got.read_bytes() == b"LOGIN-STATE"
    finally:
        config.reset_settings()
