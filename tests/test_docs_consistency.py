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
        # **不要在这里枚举类名**。原来是一份写死的清单
        # （ApiError|ProfileError|…|PackError），结果新增一个 `EngineSourceError`
        # 之后，它抛出的错误码既不会被检查、文档里也就一直缺着 ——
        # 测试看起来"在守规矩"，其实对新类完全失明。改成匹配任意 `XxxError(`。
        for m in re.finditer(
            r'[A-Za-z_][A-Za-z0-9_]*Error\(\s*(?:\d+\s*,\s*)?"([A-Z][A-Z0-9_]{2,})"',
            text,
        ):
            codes.add(m.group(1))
        # MCP 的错误码不是异常，而是塞在返回字典里的字符串
        # （`{"ok": false, "code": "MCP_MISSING_ARG", ...}`）。
        # 只扫异常构造会漏掉它们，导致"文档里记了但代码里找不到"的假失败。
        for m in re.finditer(r'"code"\s*:\s*"([A-Z][A-Z0-9_]{2,})"', text):
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


def test_hub_declares_every_third_party_import():
    """hub 的依赖必须在 pyproject 里声明。

    真踩过：代理库用 PyNaCl 加密密码，本地 venv 里装了 pynacl（core 依赖它），
    所以本地测试全绿；但 hub 镜像只装 hub 自己的依赖 → 3.5 上 `/v1/proxies`
    直接 500（`ModuleNotFoundError: No module named 'nacl'`）。
    这条测试扫描 hub 源码的第三方 import，逐个对 pyproject 的 dependencies。
    """
    import re
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    hub_src = root / "hub" / "src" / "zw_hub"
    # 用 tomllib 正经解析，别手撕字符串：
    # `"uvicorn[standard]>=..."` 里带方括号，按 "]" 切会把后半段依赖全丢掉
    # （第一版就是这么错的，报"pydantic 未声明"）。
    import tomllib

    meta = tomllib.loads((root / "hub" / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set()
    for spec in meta.get("project", {}).get("dependencies", []):
        m = re.match(r"([A-Za-z0-9_.\-]+)", spec)
        if m:
            declared.add(m.group(1).lower())

    # import 名 → 发行包名 的已知差异
    alias = {"nacl": "pynacl", "jwt": "pyjwt", "yaml": "pyyaml", "dotenv": "python-dotenv"}
    stdlib = set(sys.stdlib_module_names)
    # 本项目自己的模块 + 明确可选的
    local = {"zw_hub", "zw_core"}
    optional = {"pytest", "httpx"}

    missing: set[str] = set()
    for py in hub_src.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if not m:
                continue
            name = m.group(1)
            if name in stdlib or name in local or name in optional or name.startswith("_"):
                continue
            pkg = alias.get(name, name).lower()
            if pkg not in declared:
                missing.add(f"{name} (需要 {pkg}) ← {py.relative_to(root)}")
    assert not missing, "hub 用了未声明的依赖：\n" + "\n".join(sorted(missing))


def test_every_public_route_is_documented():
    """API 源码里注册的路由，必须在 docs/04-接口文档.md 里出现。

    AGENTS.md 的规矩是"加一个接口 → 文档 04 补一行"。但这条规矩以前没人守：
    0.7.14–0.7.27 之间加的 4 个接口（extensions/updates、proxies/sync、
    proxies/{id}/hide、devices/proxies）文档里一条都没有 —— 用户和 AI 都查不到。

    白名单：内部/调试用的路由不写文档（`/healthz`、`/metrics`、`/mcp`、`/`）。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    api_src = (root / "core/src/zw_core/api.py").read_text(encoding="utf-8")
    doc = (root / "docs/04-接口文档.md").read_text(encoding="utf-8")

    # 抓 @app.<method>(f"{API_PREFIX}/xxx"...)
    routes = set()
    for m in re.finditer(r'@app\.(get|post|put|patch|delete|websocket)\(\s*f?"\{API_PREFIX\}(/[^"]*)"', api_src):
        path = m.group(2)
        if not path:
            continue
        # 源码里 f-string 用 `{{ref}}` 表示字面量花括号，先还原成单个
        path = path.replace("{{", "{").replace("}}", "}")
        routes.add(path)

    skip = {"/healthz", "/metrics", "/mcp", "/ai/manifest"}
    missing = []
    for path in sorted(routes):
        if path in skip:
            continue
        # 文档里把 {ref}/{id} 写成字面量即可，做宽松匹配
        probe = re.sub(r"\{[a-z_]+\}", "{", path).rstrip("/")
        doc_norm = re.sub(r"\{[a-z_]+\}", "{", doc)
        if probe not in doc_norm:
            missing.append(path)
    assert not missing, "这些接口没写进 docs/04：\n" + "\n".join(missing)


def test_every_config_env_var_is_documented():
    """`config.py` 里支持的每个 `ZW_*` 环境变量，都要写进 `docs/03-部署指南.md`。

    这条抓到过 7 个漏写的，其中 4 个**所有文档都没提**：
    `ZW_ALLOW_ROOT`、`ZW_GATEWAY_ENABLED`、`ZW_INTERNAL_TOKEN`、`ZW_DEVICE_ID`。

    更严重的是其中两个是**死开关**：配置层有定义、全项目没人读 ——
    用户设了完全没效果（`ZW_ALLOW_ROOT` 与 `ZW_GATEWAY_ENABLED` 已修成真的生效）。
    文档漏写会让人根本不知道有这些开关；死开关会让知道的人也被骗。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "core/src/zw_core/config.py").read_text(encoding="utf-8")
    doc = (root / "docs/03-部署指南.md").read_text(encoding="utf-8")

    envs = sorted(set(re.findall(r'_env(?:_bool|_int|_float|_list)?\("(ZW_[A-Z0-9_]+)"', src)))
    assert len(envs) >= 40, f"只解析出 {len(envs)} 个变量，正则可能过时了"
    missing = [e for e in envs if e not in doc]
    assert not missing, "这些配置项没写进 docs/03：\n  " + "\n  ".join(missing)


def test_no_dead_config_fields():
    """`config.py` 里的配置字段，必须在**别处**真的被读过。

    这条抓到过三个"声明了但没生效"的：
      - `engine_autofetch`：注释说"缺内核时自动下载"，而自动下载**从未实现**
      - `internal_token`：注释说"gateway ↔ core 内部令牌"，但网关鉴权只用
        `admin_token` / `device_id`，没有任何地方读它
      - `gw_public_base`：与真正生效的 `gateway_public_base` property 撞名，
        两处并存只会让人改错一处

    它们都已删除。文档漏写只是让人不知道；**死配置**是让人知道了也被骗，
    而且会让排错时"我明明设了"变成一句假话。

    豁免（合理存在、只在 config.py 内部或经 getattr 使用）：
      - `force_token_on_public`：被 config 自己的 validator 读
      - `shell_cwd`：经 `getattr(s, "shell_cwd")` 读取（grep 抓不到点号形式）
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = (root / "core/src/zw_core/config.py").read_text(encoding="utf-8")
    fields = set(re.findall(
        r"^\s{4}([a-z_][a-z0-9_]*)\s*:\s*[^=]+=\s*field\(", cfg, re.M))

    others = "\n".join(
        p.read_text(encoding="utf-8")
        for p in list((root / "core/src").rglob("*.py")) + list((root / "hub/src").rglob("*.py"))
        if p.name != "config.py")

    exempt = {"force_token_on_public", "shell_cwd"}
    dead = [f for f in sorted(fields)
            if f not in exempt and not re.search(rf"\.{f}\b", others)]
    assert not dead, (
        "这些配置字段除 config.py 外没有任何地方读取（设了不生效）：\n  "
        + "\n  ".join(dead)
        + "\n要么接上实现，要么删掉并同步 docs/03。")


def test_every_custom_exception_has_a_handler():
    """项目里每个自定义异常类，都必须在 `api.py` 里有对应的 exception_handler。

    否则它会一直冒到 `@app.exception_handler(Exception)`，变成 500 + 原始堆栈 ——
    用户和 AI 拿到的是 traceback，不是 `{ok:false, code, message}`。

    真踩过：`api.py` 只注册了 8 个处理器，而项目里有 14 个自定义异常类，
    `PackError` / `StoreError` / `ShellError` / `SessionError` / `RelayError`
    全都没接住。是全量扫 AI 清单端点时从日志里的 "未处理异常" 发现的。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # 只扫 **core/src**：core 通过 HTTP 跟 hub 打交道，hub 那边的异常
    # 永远不会传播进来（hub 自己有自己的处理器）。
    # 第一版把 hub/src 也扫了，于是要求 core 给 `DeviceError`（hub 的类）注册处理器 ——
    # 那是错的，测试写错了。
    classes = set()
    for f in (root / "core/src").rglob("*.py"):
        for m in re.finditer(r"class\s+([A-Za-z_]+Error)\s*\(Exception\)", f.read_text(encoding="utf-8")):
            classes.add(m.group(1))

    api_src = (root / "core/src/zw_core/api.py").read_text(encoding="utf-8")
    # 同上：别枚举模块前缀。原来是 (?:mgr\.|syncsvc\.|…|relaysvc\.)，新模块
    # `enginesrc.` 不在表里 → 明明加了处理器也被判成"没加"。
    handled = set(re.findall(
        r"exception_handler\(\s*(?:[a-z_]+\s*,\s*)?(?:[a-z_][a-z0-9_]*\.)?([A-Za-z_]+Error)\s*\)",
        api_src))

    # `ApiError` 自己有处理器；`CryptoError` 只在 bundle 内部抛并被包成 BundleError。
    exempt = {"CryptoError"}
    missing = sorted(classes - handled - exempt)
    assert classes, "没扫到任何自定义异常类，正则可能过时了"
    assert not missing, (
        "这些异常类没有 exception_handler，会冒成 500 + traceback：\n  "
        + "\n  ".join(missing) + "\n在 api.py 里补上（照 _pack_error 的写法）。")


def test_quickstart_tells_users_engine_is_automatic():
    """快速开始不能让用户去手动下载内核。

    0.7.72 起客户端会自己下载安装 ungoogled-chromium；而快速开始里原来还留着
    "curl 下载 fingerprint-chromium 压缩包 + Expand-Archive/tar 手动解压"的旧步骤。
    那种文档比没有更糟：用户照着做，装完发现和程序自动管理的内核并存，
    反而搞不清哪个在生效。

    同时必须说明**为什么不能用系统 Chrome**（137 起忽略 --load-extension），
    否则用户会理所当然地用 `apt install chromium` / 系统里的 Chrome。
    """
    text = (ROOT / "docs/01-快速开始.md").read_text(encoding="utf-8")
    assert "engine install" in text, "快速开始没告诉用户内核是自动装的"
    assert "Expand-Archive" not in text, "还在教用户手动解压内核压缩包"
    assert "releases/download" not in text, "还在教用户手动下载内核"
    assert "137" in text and "load-extension" in text, \
        "没说清为什么不能用官方 Chrome"


def test_documented_env_vars_actually_exist_in_code():
    """文档里出现的每个 `ZW_*` 变量，代码里必须真的读它。

    用户照着部署文档配环境变量，配了个代码从不读取的名字，表现是"配了没效果" ——
    最难查的一类问题（没有任何报错，行为就是不变）。

    实测抓到过：`docs/03` 第五节写「设不同的 `ZW_PORT_POOL`」，而真实配置项是
    `ZW_PORT_POOL_START` / `ZW_PORT_POOL_END` 两个变量，压根没有 `ZW_PORT_POOL`。
    照那行做，两台 core 仍然抢同一段 CDP 端口。

    同时这条能防止另一种错：文档把默认值写错。同一份文档里
    `ZW_PORT_POOL_START` 的默认值曾同时出现 `21001` 和 `22001` 两种说法。
    """
    import pathlib
    import re

    # 只认**配置表里**的行（`| \`ZW_XXX\` | 默认值 | 说明 |`）。
    #
    # 不能全文扫：文档里还有大量非配置项的同形字符串 ——
    # 示例命令里的 `ZW_CORE` / `ZW_CHROME`、脚本变量 `ZW_EXT_DIR`、`ZW_SHOT_DIR`、
    # 以及正文里"没有叫 ZW_PORT_POOL 的配置项"这种**否定式提及**。
    # 全文扫会把这些全报成"文档有、代码没有"，噪音盖住真问题。
    #
    # 配置表才是"用户照着配"的权威清单，只查它。
    doc_vars: set[str] = set()
    for doc in (ROOT / "docs").glob("*.md"):
        for line in doc.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            for name in re.findall(r"`(ZW_[A-Z0-9_]+)`", stripped):
                doc_vars.add(name)

    code = ""
    for sub in ("core/src", "hub/src"):
        for p in (ROOT / sub).rglob("*.py"):
            code += p.read_text(encoding="utf-8", errors="ignore")
    code_vars = set(re.findall(r"\b(ZW_[A-Z0-9_]+)\b", code))

    missing = sorted(doc_vars - code_vars)
    assert not missing, (
        "配置表里列了但代码从不读取的环境变量："
        + ", ".join(missing)
        + "（用户照文档配会配了没效果，且没有任何报错）"
    )


def test_port_pool_defaults_are_consistent_in_docs():
    """同一份部署文档里，端口池默认值不能有两种说法。

    曾出现：一处写 `21001 / 21100`，另一处写 `22001 / 22100`。
    用户不知道该信哪个，而代码实际是 21001/21100。
    """
    import re

    text = (ROOT / "docs/03-部署指南.md").read_text(encoding="utf-8")
    # 只取"端口池"那几行里的默认值
    vals: set[str] = set()
    for line in text.splitlines():
        if "ZW_PORT_POOL" in line and "|" in line:
            for m in re.findall(r"`(\d{5})`", line):
                vals.add(m)
    assert vals, "文档里没找到端口池默认值"
    from zw_core.config import Settings

    s = Settings()
    assert str(s.port_pool_start) in vals, f"文档默认值与代码不符：文档={vals} 代码={s.port_pool_start}"
    assert str(s.port_pool_end) in vals, f"文档默认值与代码不符：文档={vals} 代码={s.port_pool_end}"


def test_ai_manifest_lists_all_public_routes(monkeypatch):
    """`/api/v1/ai/manifest` 的端点清单必须覆盖所有公开路由。

    AGENTS.md 写着"加一个接口 → 在 `/api/v1/ai/manifest` 的端点清单里补一条"。
    这是**给 AI 用的自描述清单** —— AI 读完它就知道全部能力。漏一条，AI 就
    不知道那个能力存在，会用别的方式硬凑或者干脆说"做不到"。

    实测漏过：`/engine`、`/engine/install`、`/packs/scan-browsers` 三条
    （都是后来加的），AI 因此不知道"内核可以自己装"、"能扫描本机浏览器导入插件"。

    做法：**真的调一次接口**拿返回，再和 FastAPI 里注册的路由表比对 ——
    比正则解析源码可靠（源码里同时有注册语句和清单文本，容易互相干扰）。
    """
    import re

    from fastapi.testclient import TestClient

    import zw_core.api as api_mod

    app = api_mod.build_app() if hasattr(api_mod, "build_app") else None
    if app is None:
        pytest.skip("拿不到 app 工厂")
    # 鉴权只认 `admin_token` 与 `device_id` 两处（见 api.py 顶部说明）
    TOKEN = "zw_manifest_audit_token"
    monkeypatch.setenv("ZW_ADMIN_TOKEN", TOKEN)
    from zw_core import config as _cfg

    _cfg.reset_settings()

    # 必须带令牌：没配凭据时非回环请求一律拒绝（这是设计，不是 bug）
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        r = c.get("/api/v1/ai/manifest")
        assert r.status_code == 200, r.text[:200]
        data = (r.json() or {}).get("data") or {}
        listed = data.get("端点清单") or []
        assert listed, "manifest 里没有 端点清单"

    norm = lambda x: re.sub(r"\{[^}]+\}", "{}", str(x)).rstrip("/")
    listed_paths = {norm(x.get("path")) for x in listed}

    real = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/v1"):
            real.add(norm(path))

    missing = sorted(real - listed_paths)
    assert not missing, (
        "这些路由没写进 ai/manifest 的端点清单：\n  " + "\n  ".join(missing)
    )


def test_delivery_checklist_is_not_stale():
    """《交付验收清单》必须跟上当前版本与门槛数字。

    这份文档是用户**照着自己点一遍**的验收单。它停在旧版本会直接骗人：
    曾经停在 v0.7.70（601 项测试、八项门槛），而当时已经是 0.7.88（642 项、九项）。
    用户按旧清单验，看到"ui-check 29 项"却发现实际跑出 37 项，会怀疑哪边不对。

    这里只校验**可机械比对**的三件事：
      1. 标题里的版本号 == 代码版本
      2. 正文声明的单元测试数 == AGENTS.md 里的数（AGENTS 有测试盯着）
      3. 正文列出的门槛条目数 == check-all.sh 里实际跑的门槛数
    """
    import re

    import zw_core

    doc = (ROOT / "docs/交付验收清单.md").read_text(encoding="utf-8")

    # ① 声明性版本号：只看**标题行**与 `版本：**x.y.z**` 这种声明处。
    #
    # 不能全文匹配：文档正文里提到历史修复版本是**合理**的
    # （"0.7.83 修过把'直连'显示错的问题"），而 `0.8078.79` 是 Chromium 版本。
    # 全文匹配会把这两类都报成"过期"，噪音盖住真问题。
    # 第一版我甚至只取第一处匹配，结果故意改错标题都抓不到 —— 两个极端都试过了。
    declared = set(re.findall(r"^#.*?v(\d+\.\d+\.\d+)", doc, re.M))
    declared |= set(re.findall(r"^版本：\*\*(\d+\.\d+\.\d+)\*\*", doc, re.M))
    assert declared, "交付清单里找不到声明性的版本号"
    stale = sorted(v for v in declared if v != zw_core.__version__)
    assert not stale, (
        f"交付清单声明的版本是 {stale}，当前是 {zw_core.__version__}"
    )

    # ② 单元测试数：与 AGENTS.md 声明一致
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    am = re.search(r"tests/\s+(\d+) 项单元测试", agents)
    assert am, "AGENTS.md 里没找到测试数声明"
    n_agents = am.group(1)
    assert f"**{n_agents} 项**" in doc or f"{n_agents} passed" in doc, (
        f"交付清单没写当前测试数 {n_agents}"
    )

    # ③ 门槛条目数：check-all.sh 里有几个 `pass "xxx"` 就是几项
    sh = (ROOT / "scripts/check-all.sh").read_text(encoding="utf-8")
    gates = set(re.findall(r'^\s*pass "([a-z0-9\-]+)"', sh, re.M))
    listed = set(re.findall(r"^([a-z0-9\-]+)\s+✅", doc, re.M))
    missing = sorted(gates - listed)
    assert not missing, (
        "交付清单里缺这些门槛的结果行：" + ", ".join(missing)
    )
