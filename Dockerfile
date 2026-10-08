# 每日 AI 新闻助手 Agent — 生产镜像
# 基础镜像：python 3.10-slim（和本地 3.10.8 对齐，slim 体积小）
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 系统依赖（uvicorn standard 需要，gcc 给某些包编译兜底）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker layer cache，依赖不变不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝代码（代码变频繁，放后面）
COPY app/ ./app/
COPY static/ ./static/
COPY schema.sql .

# 数据持久化目录（briefings 简报 + data.db）
RUN mkdir -p /app/briefings
VOLUME ["/app/briefings", "/app/data"]

# 环境变量默认值（生产应通过 docker-compose env 或 -e 覆盖）
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    LOG_FORMAT=json \
    PYTHONUNBUFFERED=1

# 暴露端口
EXPOSE 8000

# 健康检查（FastAPI /health 路由）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# 启动命令（生产不开 --reload）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
