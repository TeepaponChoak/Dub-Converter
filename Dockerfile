FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# One worker: job state is kept in memory. Threads handle concurrency.
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 120
