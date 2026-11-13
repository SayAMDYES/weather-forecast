FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 glow \
    && useradd --system --uid 10001 --gid glow --create-home glow

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

RUN apt-get update \
    && apt-get install --no-install-recommends -y libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=glow:glow . .
RUN mkdir -p data \
    && chown -R glow:glow /app

USER glow

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=3)"

CMD ["python", "-m", "glow", "serve"]
