# 知纹同步中心 · 单容器镜像
#
# ## 两个刻意的写法，别"优化"掉
#
# 1. **`pip install` 后面绝不加 `|| true`。**
#    以前这里写的是 `RUN pip install . && pip uninstall -y zw-hub || true`，
#    结果是：构建机网络抖一下、pip 装到一半断了，`|| true` 把失败吞掉，
#    镜像照样"构建成功"——但里面**一个依赖都没有**。容器起不来，
#    报的是 `ModuleNotFoundError: No module named 'fastapi'`，
#    而且 `docker compose up` 看起来一切正常（只有 restart 计数在涨）。
#    这个坑真踩过：3.5 上的 hub 因此挂过一次。
# 2. **不要 uninstall 自己。** 原先卸掉 zw-hub 是为了"不留元数据"，
#    但 `COPY src` + `PYTHONPATH=/app/src` 已经让源码生效，
#    卸包只增加一步可能失败的操作。留着包还能用 `zw-hub` 这个入口命令。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Hong_Kong

WORKDIR /app

COPY pyproject.toml ./
# README 是 pyproject 里 readme 字段可能引用的；缺了会让 pip 报错，
# 所以先放一份，避免"本地能装、容器装不上"这类差异。
COPY README.md ./
COPY src ./src

# 依赖自检里带上 nacl：代理库的密码加密用到它，
# 曾经因为"本地 venv 有、镜像里没有"导致 3.5 上 /v1/proxies 直接 500。
# 构建阶段就 import 一次，缺了立刻失败，不要等运行期。
#
# 网络重试：构建机连 PyPI 偶发断流（实测 355 秒后 BrokenPipe）。
# 加 retries/timeout 让构建更稳，但**失败必须是失败**，不能静默跳过。
RUN pip install --no-cache-dir --retries 5 --timeout 60 . \
 && python -c "import fastapi, uvicorn, pydantic, nacl; print('deps ok:', fastapi.__version__)"

# 双保险：源码路径优先（改代码不用重装）
ENV PYTHONPATH=/app/src

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8700

HEALTHCHECK --interval=20s --timeout=4s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/healthz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "zw_hub.main"]
