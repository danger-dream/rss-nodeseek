# 使用Python 3.9官方镜像作为基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用程序文件
COPY rss_main.py .

# 创建数据目录
RUN mkdir -p /data

# 设置数据卷
VOLUME ["/data"]

# 设置默认命令
CMD ["python", "rss_main.py"]

# 添加标签
LABEL maintainer="RSS Monitor Team" \
      description="RSS监控程序 - 监控RSS源并通过Telegram发送通知" \
      version="1.0" 