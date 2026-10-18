# ---- web UI ---------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- app ------------------------------------------------------------------------------
# Ubuntu 24.04: Python 3.12 + ffmpeg 6.1 with libass (the versions AutoReel is tested with).
FROM ubuntu:24.04

# Set to "true" to include the optional browser-automation TikTok uploader (+~400 MB).
ARG TIKTOK_BROWSER=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUTOREEL_DATA_DIR=/data \
    AUTOREEL_HOST=0.0.0.0 \
    AUTOREEL_PORT=8000 \
    AUTOREEL_FRONTEND_DIST=/app/frontend/dist \
    HF_HOME=/data/models/hf \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv ffmpeg fonts-dejavu-core tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml README.md ./
COPY autoreel ./autoreel
RUN if [ "$TIKTOK_BROWSER" = "true" ]; then \
      pip install ".[tiktok-browser]" && playwright install --with-deps chromium; \
    else \
      pip install .; \
    fi
COPY --from=web /web/dist ./frontend/dist

RUN useradd --create-home --uid 10001 autoreel && mkdir -p /data && chown autoreel:autoreel /data
USER autoreel
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

ENTRYPOINT ["tini", "--"]
CMD ["autoreel", "serve"]
