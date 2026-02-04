# Parlant 依赖安装说明

## 📦 Dockerfile 中的依赖安装

### 关键命令（第 44 行）

```dockerfile
RUN uv pip install --system -e .
```

这个命令会：
1. 读取 `pyproject.toml` 文件
2. 安装 Parlant 包（从 `src/parlant/` 目录）
3. 自动安装 **60+ 个依赖包**

---

## 📋 完整依赖列表

### 从 pyproject.toml 自动安装的包：

#### Web 框架和服务器
- `fastapi>=0.120.0` - 现代 Web 框架
- `uvicorn>=0.38.0` - ASGI 服务器
- `starlette>=0.49.0` - Web 工具包

#### LLM 客户端
- `openai>=2.8.0` - OpenAI API 客户端
- `parlant-client>=3.1.0` - Parlant 客户端库

#### 分词和文本处理
- `tiktoken>=0.12` - OpenAI 分词器
- `tokenizers>=0.21` - HuggingFace 分词器

#### 异步和 I/O
- `aiofiles>=24.1.0` - 异步文件操作
- `aiorwlock>=1.5.0` - 异步读写锁
- `httpx>=0.28.1` - 异步 HTTP 客户端

#### 数据库和存储
- `nano-vectordb>=0.0.4.3` - 向量数据库
- `pymongo>=4.11.1` - MongoDB 客户端（可选）
- `qdrant-client>=1.7.0` - Qdrant 客户端（可选）

#### 日志和监控
- `structlog>=24.4.0` - 结构化日志
- `rich>=14.0.0` - 终端美化
- `coloredlogs>=15.0.1` - 彩色日志
- `opentelemetry-api>=1.37.0` - 遥测 API
- `opentelemetry-sdk>=1.37.0` - 遥测 SDK

#### 工具和实用程序
- `click>=8.1.7` - CLI 工具
- `python-dotenv>=1.0.1` - 环境变量
- `requests>=2.32.5` - HTTP 请求
- `jinja2>=3.1.6` - 模板引擎
- `jsonschema>=4.23.0` - JSON 验证
- `networkx[default]>=3.3` - 图算法
- `more-itertools>=10.3.0` - 迭代工具

#### 其他核心依赖
- `boto3>=1.35.70` - AWS SDK
- `cachetools>=6.0.0` - 缓存工具
- `croniter>=5.0.1` - Cron 表达式
- `authlib>=1.6.5` - 认证库
- `limits>=5.5.0` - 速率限制
- `mcp>=1.16.0` - MCP 协议
- `nanoid>=2.0.0` - ID 生成器
- `semver>=3.0.2` - 语义版本
- `tabulate>=0.9.0` - 表格格式化
- `websocket-client>=1.5.3` - WebSocket 客户端

**总计：60+ 个依赖包**

---

## 🔍 验证安装

### 方法1：构建时验证

Dockerfile 第 47 行会在构建时验证：
```dockerfile
RUN python -c "import parlant; print(f'Parlant {parlant.__version__} installed successfully')"
```

### 方法2：运行时验证

```bash
# 1. 构建镜像
docker build -t parlant-base:latest .

# 2. 启动容器
docker run -d --name parlant-verify parlant-base:latest

# 3. 运行验证脚本
docker exec parlant-verify bash /app/verify-installation.sh

# 4. 手动验证
docker exec -it parlant-verify bash

# 在容器内执行：
python -c "import parlant; print(parlant.__version__)"
pip list | wc -l  # 查看安装的包数量
pip show parlant  # 查看 Parlant 详细信息
```

---

## 🚀 完整构建流程

### 1. 构建镜像

```bash
cd /mnt/e/work/package/parlant
docker build -t parlant-base:latest .
```

**构建输出示例：**
```
Step 8/12 : RUN uv pip install --system -e .
 ---> Running in abc123def456...
Resolved 65 packages in 2.3s
Downloaded 65 packages in 5.1s
Installing...
  ✓ aiofiles-24.1.0
  ✓ fastapi-0.120.0
  ✓ openai-2.8.0
  ✓ tiktoken-0.12.0
  ✓ structlog-24.4.0
  ... (60+ more packages)
Successfully installed parlant-3.1.2
 ---> abc123def456

Step 9/12 : RUN python -c "import parlant; print(f'Parlant {parlant.__version__} installed successfully')"
 ---> Running in def456ghi789...
Parlant 3.1.2 installed successfully
 ---> def456ghi789
```

### 2. 验证安装

```bash
# 启动容器
docker run -d --name test-parlant parlant-base:latest

# 进入容器
docker exec -it test-parlant bash

# 验证 Parlant
python << EOF
import parlant
import parlant.sdk as p
print(f"Parlant version: {parlant.__version__}")
print(f"Parlant SDK imported successfully")
EOF

# 验证命令行工具
parlant-server --help
parlant --help

# 查看所有依赖
pip list

# 查看特定包
pip show parlant
pip show fastapi
pip show openai
```

---

## 📊 依赖关系图

```
parlant (3.1.2)
├── fastapi (0.120.0)
│   ├── starlette
│   └── pydantic
├── openai (2.8.0)
│   └── httpx
├── tiktoken (0.12)
├── structlog (24.4.0)
├── rich (14.0.0)
├── uvicorn (0.38.0)
├── networkx (3.3)
├── jinja2 (3.1.6)
├── jsonschema (4.23.0)
└── ... (50+ more dependencies)
```

---

## 🎯 常见问题

### Q1: 为什么使用 `uv` 而不是 `pip`？
**A:** uv 是用 Rust 编写的，比 pip 快 10-100 倍，特别适合 Docker 构建。

### Q2: `-e .` 是什么意思？
**A:**
- `-e` = editable mode（可编辑模式）
- `.` = 当前目录
- 会从当前目录的 `pyproject.toml` 安装包

### Q3: 如何查看具体安装了哪些包？
**A:**
```bash
docker run --rm parlant-base:latest pip list
```

### Q4: 如何添加额外的依赖？
**A:** 在 Dockerfile 中添加：
```dockerfile
RUN uv pip install --system your-package-name
```

### Q5: 构建失败怎么办？
**A:**
```bash
# 查看详细日志
docker build --progress=plain -t parlant-base:latest .

# 不使用缓存重新构建
docker build --no-cache -t parlant-base:latest .
```

---

## 📝 总结

✅ **Dockerfile 第 44 行** 会自动安装 Parlant 及其 60+ 个依赖
✅ **第 47 行** 验证安装是否成功
✅ **第 50-54 行** 额外安装 Flask 生态
✅ 使用 **uv** 包管理器加速安装
✅ 所有依赖定义在 **pyproject.toml** 中

现在您可以放心构建镜像，所有依赖都会自动安装！🎉
