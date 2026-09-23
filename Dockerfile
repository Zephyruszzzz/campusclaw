FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY campusclaw/ ./campusclaw/
COPY app.py ./
COPY scripts/ ./scripts/

# 数据与上传目录由 Compose 挂载为宿主机持久化目录。
RUN mkdir -p /app/data /app/uploads

EXPOSE 8080

# 单 worker：课程演示环境，不宣称高并发能力。
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8080", "app:app"]
