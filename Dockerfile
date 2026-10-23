FROM node:22-alpine3.22
RUN apk add --no-cache python3 git tini && addgroup -g 10001 worker && adduser -D -u 10001 -G worker worker
WORKDIR /app
COPY worker.py /app/worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 USEVPS_ROOT=/workspace
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "/app/worker.py", "serve"]
LABEL eu.progsoft.usevps.image=true
