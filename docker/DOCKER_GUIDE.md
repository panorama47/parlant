# Parlant + Flask Docker部署指南

## 📦 项目包含的Docker文件

### 1. **Dockerfile** - Parlant基础镜像
- 仅包含Parlant和Flask基础依赖
- 适合作为基础镜像使用
- 镜像大小: ~500MB

### 2. **Dockerfile.flask** - 完整应用镜像
- 包含Parlant + Flask完整应用
- 多阶段构建，优化镜像大小
- 生产级配置（gunicorn + gevent）
- 镜像大小: ~600MB

### 3. **docker-compose.yml** - 完整技术栈
- Parlant服务器
- Flask应用
- MongoDB（持久化存储）
- Qdrant（向量数据库）

---

## 🚀 快速开始

### 方式1: 使用Docker Compose（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/emcie-co/parlant.git
cd parlant

# 2. 配置环境变量
cp .env.example .env
# 编辑.env文件，填入你的API密钥

# 3. 启动所有服务
docker-compose up -d

# 4. 查看日志
docker-compose logs -f

# 5. 访问服务
# Parlant服务器: http://localhost:8800
# Flask应用: http://localhost:5000
# MongoDB: localhost:27017
# Qdrant: http://localhost:6333
```

### 方式2: 仅构建Parlant基础镜像

```bash
# 构建镜像
docker build -t parlant-base:latest -f Dockerfile .

# 运行容器
docker run -d \
  --name parlant-server \
  -p 8800:8800 \
  -e OPENAI_API_KEY=your-key-here \
  parlant-base:latest
```

### 方式3: 构建Flask完整应用

```bash
# 构建镜像
docker build -t parlant-flask:latest -f Dockerfile.flask .

# 运行容器
docker run -d \
  --name parlant-flask-app \
  -p 5000:5000 \
  -e OPENAI_API_KEY=your-key-here \
  -e PARLANT_SERVER_URL=http://parlant-server:8800 \
  parlant-flask:latest
```

---

## 🔧 配置说明

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `OPENAI_API_KEY` | OpenAI API密钥 | 必填 |
| `ANTHROPIC_API_KEY` | Anthropic API密钥 | 可选 |
| `PARLANT_DB_TYPE` | 数据库类型 | `transient` |
| `PARLANT_SERVER_URL` | Parlant服务器地址 | `http://localhost:8800` |
| `FLASK_ENV` | Flask环境 | `production` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

### 数据库选项

```yaml
# 使用内存数据库（默认，重启后数据丢失）
PARLANT_DB_TYPE=transient

# 使用MongoDB（推荐生产环境）
PARLANT_DB_TYPE=mongodb
DATABASE_URL=mongodb://parlant:password@mongodb:27017/parlant

# 使用JSON文件
PARLANT_DB_TYPE=json_file
PARLANT_DB_PATH=/app/data/parlant.json
```

---

## 📊 服务架构

```
┌─────────────────┐
│   用户请求      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Flask应用      │  Port 5000
│  (REST API)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Parlant服务器   │  Port 8800
│  (AI引擎)       │
└────┬────────┬───┘
     │        │
     ▼        ▼
┌─────────┐ ┌──────────┐
│ MongoDB │ │  Qdrant  │
│ (数据)  │ │ (向量DB) │
└─────────┘ └──────────┘
```

---

## 🛠️ 开发与调试

### 查看日志

```bash
# 所有服务
docker-compose logs -f

# 特定服务
docker-compose logs -f parlant-server
docker-compose logs -f flask-app
```

### 进入容器

```bash
# 进入Parlant服务器
docker-compose exec parlant-server bash

# 进入Flask应用
docker-compose exec flask-app bash
```

### 重启服务

```bash
# 重启所有服务
docker-compose restart

# 重启特定服务
docker-compose restart flask-app
```

### 停止和清理

```bash
# 停止所有服务
docker-compose down

# 停止并删除数据卷
docker-compose down -v

# 停止并删除镜像
docker-compose down --rmi all
```

---

## 🔒 生产环境部署

### 1. 安全配置

```bash
# 生成强密钥
openssl rand -hex 32

# 在.env中设置
SECRET_KEY=生成的密钥
```

### 2. 使用HTTPS

```yaml
# 在docker-compose.yml中添加nginx反向代理
nginx:
  image: nginx:alpine
  ports:
    - "80:80"
    - "443:443"
  volumes:
    - ./nginx.conf:/etc/nginx/nginx.conf
    - ./ssl:/etc/nginx/ssl
```

### 3. 资源限制

```yaml
services:
  flask-app:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
```

### 4. 健康检查

所有服务都配置了健康检查：

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
  interval: 30s
  timeout: 10s
  retries: 3
```

---

## 📈 性能优化

### Gunicorn配置

```bash
# 在docker-compose.yml中调整workers数量
environment:
  - GUNICORN_WORKERS=4  # CPU核心数 * 2 + 1
  - GUNICORN_THREADS=2
  - GUNICORN_TIMEOUT=120
```

### 数据库连接池

```python
# 在config.py中配置
SQLALCHEMY_POOL_SIZE = 10
SQLALCHEMY_MAX_OVERFLOW = 20
```

---

## 🧪 测试

### API测试

```bash
# 健康检查
curl http://localhost:5000/health

# 列出智能体
curl http://localhost:5000/api/agents

# 发送消息
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-1",
    "message": "你好",
    "customer_id": "test-user"
  }'
```

---

## 🐛 故障排查

### 常见问题

1. **容器无法启动**
   ```bash
   # 查看详细日志
   docker-compose logs parlant-server

   # 检查环境变量
   docker-compose config
   ```

2. **API密钥错误**
   ```bash
   # 确认环境变量已设置
   docker-compose exec flask-app env | grep API_KEY
   ```

3. **数据库连接失败**
   ```bash
   # 检查MongoDB是否运行
   docker-compose ps mongodb

   # 测试连接
   docker-compose exec mongodb mongosh
   ```

4. **端口冲突**
   ```bash
   # 修改docker-compose.yml中的端口映射
   ports:
     - "5001:5000"  # 使用其他端口
   ```

---

## 📚 更多资源

- [Parlant官方文档](https://parlant.io/docs)
- [Flask文档](https://flask.palletsprojects.com/)
- [Docker最佳实践](https://docs.docker.com/develop/dev-best-practices/)
- [Gunicorn配置](https://docs.gunicorn.org/en/stable/settings.html)

---

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

Apache 2.0 License
