# 知纹同步中心 · 单容器镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Hong_Kong

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && pip uninstall -y zw-hub 2>/dev/null || true
# 上面 uninstall 只是为了不留多余元数据；直接用源码路径运行
ENV PYTHONPATH=/app/src

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8700

HEALTHCHECK --interval=20s --timeout=4s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/healthz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "zw_hub.main"]
