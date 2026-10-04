FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/renewal_radar.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 radar && mkdir -p /data && chown radar:radar /data
USER radar
EXPOSE 8000
CMD ["renewal-radar", "serve", "--host", "0.0.0.0", "--port", "8000"]
