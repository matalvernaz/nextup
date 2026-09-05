FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

# Runs as an unprivileged user; /data is the only writable path it needs.
RUN useradd -u 1000 -m nextup && mkdir -p /data && chown nextup:nextup /data
USER nextup

EXPOSE 8080
# The doctor is reachable as `docker compose run --rm nextup python -m app.doctor`
# without overriding anything else about the image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
