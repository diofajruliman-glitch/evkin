FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements-lock.txt ./backend/
RUN python -m pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY assets/ ./assets/
COPY index.html ./
RUN mkdir -p /var/lib/evkin && chown 10001:10001 /var/lib/evkin

USER 10001:10001
CMD ["python", "-m", "uvicorn", "backend.evkin_export_api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
