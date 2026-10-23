FROM alpine:3.22 AS layout
RUN mkdir -p /rootfs/app /rootfs/usr /rootfs/bin /rootfs/sbin /rootfs/lib /rootfs/lib64 /rootfs/etc /rootfs/opt /rootfs/tmp /rootfs/var /rootfs/run /rootfs/workspace /rootfs/volume-stat

FROM scratch
COPY --from=layout /rootfs/ /
COPY worker.py AGENTS.md /app/
COPY ops/toolchain-lock.json /app/toolchain-lock.json
COPY ops/worker_storage_guard.py /Users/guillaumegaillard/.local/bin/storage-guard.py
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 USEVPS_ROOT=/workspace \
    NODE_PATH=/usr/local/lib/node_modules PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 PUPPETEER_SKIP_DOWNLOAD=true \
    CHROMIUM_PATH=/usr/local/bin/chromium CHROME_BIN=/usr/local/bin/chromium \
    PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chromium UV_PYTHON_DOWNLOADS=never \
    JAVA_HOME=/usr/lib/jvm/default-java LANG=C.UTF-8
USER 10001:10001
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "echo 500 > /proc/self/oom_score_adj && exec python3 /app/worker.py serve"]
LABEL eu.progsoft.usevps.image=true
