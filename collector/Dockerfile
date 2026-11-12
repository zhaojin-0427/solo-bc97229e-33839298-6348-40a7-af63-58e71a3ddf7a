FROM python:3.11-slim-bookworm

WORKDIR /app

# 仅安装 Python 依赖；Chromium 运行在独立 browser 容器中，本镜像不装浏览器。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data \
    PORTAL_URL=http://portal:8000 \
    BROWSER_CDP_URL=http://browser:9222 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python", "-m", "app"]
