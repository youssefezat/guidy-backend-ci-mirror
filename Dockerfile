FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# GTFS_PATH defaults to internal /app/gtfs_data, or can be mounted externally
ENV GTFS_PATH=/app/gtfs_data
ENV USE_OSRM=false
ENV PORT=8000

EXPOSE 8000

# Supports dynamic PORT assigned by Cloud Run, Railway, etc., defaulting to 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
