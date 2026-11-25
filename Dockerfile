FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# httpx and nothing else: this process is an HTTP client, not a browser.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The relay's own state volume. Created here so a `docker run` without a volume
# still starts; compose mounts `relay` over it.
RUN mkdir -p /data

CMD ["python", "-m", "app.relayd"]
