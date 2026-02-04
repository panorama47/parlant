# Parlant + Flask 生产环境基础镜像
# 基于官方Python镜像构建
FROM python:3.10-slim-bookworm

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    git \
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxmlsec1-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装uv包管理器（更快的pip替代品）
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    chmod +x /root/.local/bin/uv
ENV PATH="/root/.local/bin:$PATH"

# 验证uv安装
RUN uv --version

# 复制项目文件
COPY ../pyproject.toml ../uv.lock ../README.md ./
COPY ../src/ ./src/
COPY ../scripts/ ./scripts/

# 安装Parlant及其所有依赖（从pyproject.toml读取）
# 这会安装60+个依赖包，包括：
# - fastapi, uvicorn (Web框架)
# - openai, anthropic (LLM客户端)
# - tiktoken, tokenizers (分词器)
# - pymongo, qdrant-client (数据库)
# - structlog, rich (日志)
# 等等...
RUN uv pip install --system -e .

# 验证Parlant安装
RUN python -c "import parlant; print(f'Parlant {parlant.__version__} installed successfully')"

# 安装Flask和常用扩展
RUN uv pip install --system \
    flask==3.0.0 \
    flask-cors==4.0.0 \
    gunicorn==21.2.0 \
    gevent==24.2.1

# 创建非root用户
RUN useradd -m -u 1000 parlant && \
    chown -R parlant:parlant /app

USER parlant

# 暴露端口
EXPOSE 8800 5000

# 健康检查（由于使用sleep infinity，暂时禁用健康检查）
# HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
#     CMD curl -f http://localhost:8800/health || exit 1

# 默认启动命令（保持容器运行，方便调试）
CMD ["sleep", "infinity"]
