# Pericos agent — small, non-root, one process.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change doesn't rebuild the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# The SQLite queue lives here. Mount a volume on /data in production or a
# redeploy loses anything still waiting to be written to the sheet.
RUN mkdir -p /data && \
    useradd --create-home --uid 10001 pericos && \
    chown -R pericos:pericos /app /data
USER pericos

ENV DB_PATH=/data/pericos.db \
    TZ=America/Bogota

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=5).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
