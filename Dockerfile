FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite database lives here by default (mount a volume to persist it).
ENV DATABASE_PATH=/data/app.db
RUN mkdir -p /data

EXPOSE 8000

# The app creates/initializes the SQLite schema on startup (lifespan),
# so this single command covers dependencies, DB init, and serving.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
