# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DRILL_HOST=0.0.0.0 \
    DRILL_PORT=8008 \
    DRILL_SAMPLES=3

WORKDIR /app

# Flask is installed from the vendored wheels so the image builds and runs
# without network access.
COPY vendor/ /wheels/
RUN pip install --no-cache-dir --no-index --find-links=/wheels flask==3.1.3

COPY drill/ /app/drill/
COPY sample/ /app/sample/
COPY scripts/ /app/scripts/

EXPOSE 8008

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/healthz', timeout=2)"

CMD ["python", "-m", "drill.run"]
