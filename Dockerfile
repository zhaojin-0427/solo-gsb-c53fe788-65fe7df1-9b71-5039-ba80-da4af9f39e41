FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DMX_DB_PATH=/data/dmx.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 与宿主机当前用户一致的 UID，保证绑定挂载 ./data 可写
RUN useradd -r -u 1000 -d /app appuser

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser static ./static

RUN mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
