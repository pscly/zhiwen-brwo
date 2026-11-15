# 知纹核心 · 容器镜像（可选部署方式，Windows 上建议裸装）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Hong_Kong \
    ZW_ROOT=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && pip uninstall -y zw-core 2>/dev/null || true
ENV PYTHONPATH=/app/src

# 容器里跑浏览器必须 --no-sandbox，这是安全上的取舍（只在内网/自用场景接受）
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8730

HEALTHCHECK --interval=20s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8730/healthz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "zw_core.main"]
