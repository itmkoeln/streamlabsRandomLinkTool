FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/data.db \
    PORT=8080 \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 app \
    && mkdir /data && chown app /data

WORKDIR /app
COPY app.py .
COPY locales ./locales

USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

CMD ["python", "app.py"]
