# 独立 Chromium 容器：通过 CDP 暴露给采集服务使用
FROM mcr.microsoft.com/playwright/python:v1.65.0-noble

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 以 headless 模式常驻，监听容器内 9222 的 CDP 端点
CMD ["/entrypoint.sh"]

HEALTHCHECK --interval=5s --timeout=3s --retries=10 --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=2).status==200 else 1)"
