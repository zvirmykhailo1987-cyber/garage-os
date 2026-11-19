# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sto \
 && mkdir -p /data && chown sto:sto /data
USER sto

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Proxy headers are trusted only from 127.0.0.1 by default, so X-Forwarded-For cannot be
# spoofed to dodge the login rate limit. Behind a reverse proxy in another container set
# FORWARDED_ALLOW_IPS to that proxy's address — otherwise every client shares one bucket.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
