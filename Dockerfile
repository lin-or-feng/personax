# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

LABEL org.opencontainers.image.title="PersonaX" \
      org.opencontainers.image.description="Local content and RAG Agent workbench" \
      org.opencontainers.image.source="https://github.com/lin-or-feng/personax"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/personax

WORKDIR /app

RUN addgroup --system personax \
    && adduser --system --ingroup personax --home /home/personax personax

COPY requirements-docker.txt pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir -r requirements-docker.txt

COPY . .
RUN python -m pip install --no-cache-dir --no-deps . \
    && mkdir -p logs .rag_cache content_bank knowledge assets/covers \
    && chown -R personax:personax /app /home/personax

USER personax

EXPOSE 8501 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"]

CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]
