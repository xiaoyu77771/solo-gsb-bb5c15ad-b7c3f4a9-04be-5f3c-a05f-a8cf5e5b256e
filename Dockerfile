FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 容器内固定监听 8000; 主机映射端口由 compose 的 HOST_PORT 控制。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
