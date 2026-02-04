# 📦 Parlant + Flask Docker 完整方案总结

## ✅ 已创建的文件

### 🐳 Docker相关文件

1. **Dockerfile** - Parlant基础镜像
   - 基于Python 3.10-slim
   - 包含Parlant + Flask基础依赖
   - 使用uv包管理器加速安装
   - 镜像大小: ~500MB

2. **Dockerfile.flask** - 完整应用镜像
   - 多阶段构建优化
   - 包含Gunicorn + Gevent生产配置
   - 非root用户运行
   - 镜像大小: ~600MB

3. **docker-compose.yml** - 完整技术栈编排
   - Parlant服务器 (端口8800)
   - Flask应用 (端口5000)
   - MongoDB数据库 (端口27017)
   - Qdrant向量数据库 (端口6333)
   - 健康检查和自动重启

4. **.dockerignore** - 优化构建
   - 排除不必要的文件
   - 减小镜像大小

### 🔧 配置文件

5. **.env.example** - 环境变量模板
   - API密钥配置
   - 数据库连接
   - 日志和CORS设置

6. **flask_app/config.py** - Flask配置
   - 开发/生产/测试环境配置
   - 数据库和安全设置

### 💻 应用代码

7. **flask_app/app.py** - Flask REST API示例
   - `/health` - 健康检查
   - `/api/chat` - 对话接口
   - `/api/agents` - 智能体列表
   - `/api/guidelines` - 指南管理
   - `/api/sessions` - 会话管理

8. **flask_app/app_with_sdk.py** - Parlant SDK集成示例
   - 完整的SDK使用示例
   - 异步处理
   - 会话管理

### 📚 文档

9. **DOCKER_GUIDE.md** - 详细部署指南
   - 快速开始
   - 配置说明
   - 故障排查
   - 生产部署

10. **start-docker.sh** - 一键启动脚本
    - 自动检查依赖
    - 创建配置文件
    - 构建和启动服务
    - 健康检查

---

## 🚀 快速使用

### 方法1: 一键启动（最简单）

```bash
# 1. 进入项目目录
cd parlant

# 2. 运行启动脚本
./start-docker.sh

# 脚本会自动:
# - 检查Docker环境
# - 创建.env配置文件
# - 构建镜像
# - 启动所有服务
# - 执行健康检查
```

### 方法2: 手动启动

```bash
# 1. 配置环境变量
cp .env.example .env
nano .env  # 填入你的API密钥

# 2. 启动服务
docker-compose up -d

# 3. 查看日志
docker-compose logs -f
```

### 方法3: 仅构建基础镜像

```bash
# 构建Parlant基础镜像
docker build -t parlant-base:latest -f Dockerfile .

# 运行
docker run -d \
  --name parlant \
  -p 8800:8800 \
  -e OPENAI_API_KEY=your-key \
  parlant-base:latest
```

---

## 📊 服务架构

```
┌──────────────────────────────────────────┐
│           用户/客户端                     │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│      Nginx (可选，用于HTTPS)              │
│      Port 80/443                          │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│      Flask应用 (REST API)                 │
│      Port 5000                            │
│      - Gunicorn (4 workers)               │
│      - Gevent (异步处理)                  │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│      Parlant服务器 (AI引擎)               │
│      Port 8800                            │
│      - Guideline匹配                      │
│      - Journey导航                        │
│      - 工具调用                           │
│      - 消息生成                           │
└──────┬───────────────┬───────────────────┘
       │               │
       ▼               ▼
┌─────────────┐  ┌──────────────┐
│  MongoDB    │  │   Qdrant     │
│  (数据存储) │  │  (向量数据库) │
│  Port 27017 │  │  Port 6333   │
└─────────────┘  └──────────────┘
```

---

## 🎯 API端点

### Flask应用 (http://localhost:5000)

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/api/agents` | GET | 列出所有智能体 |
| `/api/chat` | POST | 发送消息给智能体 |
| `/api/sessions/{id}` | GET | 获取会话历史 |
| `/api/guidelines` | GET | 列出所有指南 |
| `/api/guidelines` | POST | 创建新指南 |
| `/api/tools` | GET | 列出可用工具 |

### 示例请求

```bash
# 健康检查
curl http://localhost:5000/health

# 发送消息
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-1",
    "message": "我想了解产品信息",
    "customer_id": "user-123"
  }'

# 创建指南
curl -X POST http://localhost:5000/api/guidelines \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-1",
    "condition": "用户询问价格",
    "action": "提供详细的价格信息和优惠活动"
  }'
```

---

## 🔧 配置选项

### 数据库选择

#### 1. 内存数据库（默认，开发用）
```yaml
environment:
  - PARLANT_DB_TYPE=transient
```

#### 2. MongoDB（推荐生产环境）
```yaml
environment:
  - PARLANT_DB_TYPE=mongodb
  - DATABASE_URL=mongodb://parlant:password@mongodb:27017/parlant
```

#### 3. JSON文件
```yaml
environment:
  - PARLANT_DB_TYPE=json_file
  - PARLANT_DB_PATH=/app/data/parlant.json
volumes:
  - ./data:/app/data
```

### 向量数据库选择

#### 1. 内存向量库（默认）
```yaml
environment:
  - PARLANT_VECTOR_DB=transient
```

#### 2. Qdrant（推荐生产环境）
```yaml
environment:
  - PARLANT_VECTOR_DB=qdrant
  - QDRANT_URL=http://qdrant:6333
```

#### 3. Chroma
```yaml
environment:
  - PARLANT_VECTOR_DB=chroma
  - CHROMA_URL=http://chroma:8000
```

---

## 🔒 生产环境清单

### 安全配置

- [ ] 设置强SECRET_KEY
- [ ] 使用环境变量管理敏感信息
- [ ] 启用HTTPS（使用Nginx反向代理）
- [ ] 配置防火墙规则
- [ ] 限制CORS来源
- [ ] 使用非root用户运行容器

### 性能优化

- [ ] 调整Gunicorn workers数量（CPU核心数 * 2 + 1）
- [ ] 配置数据库连接池
- [ ] 启用Redis缓存（可选）
- [ ] 设置资源限制（CPU、内存）
- [ ] 配置日志轮转

### 监控和日志

- [ ] 配置日志聚合（ELK/Loki）
- [ ] 设置监控告警（Prometheus/Grafana）
- [ ] 配置健康检查端点
- [ ] 启用APM追踪（可选）

### 备份和恢复

- [ ] 配置数据库自动备份
- [ ] 测试恢复流程
- [ ] 设置数据卷持久化

---

## 📈 扩展方案

### 水平扩展

```yaml
services:
  flask-app:
    deploy:
      replicas: 3  # 运行3个实例

  nginx:
    image: nginx:alpine
    # 配置负载均衡
```

### 添加Redis缓存

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
```

### 添加Nginx反向代理

```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./ssl:/etc/nginx/ssl
```

---

## 🐛 常见问题

### 1. 容器无法启动

```bash
# 查看详细日志
docker-compose logs parlant-server

# 检查配置
docker-compose config

# 重新构建
docker-compose build --no-cache
```

### 2. API密钥错误

```bash
# 检查环境变量
docker-compose exec flask-app env | grep API_KEY

# 重新设置
docker-compose down
# 编辑.env文件
docker-compose up -d
```

### 3. 端口冲突

```yaml
# 修改docker-compose.yml
services:
  flask-app:
    ports:
      - "5001:5000"  # 使用其他端口
```

### 4. 数据库连接失败

```bash
# 检查MongoDB状态
docker-compose ps mongodb

# 查看MongoDB日志
docker-compose logs mongodb

# 测试连接
docker-compose exec mongodb mongosh
```

---

## 📚 相关资源

- [Parlant官方文档](https://parlant.io/docs)
- [Flask文档](https://flask.palletsprojects.com/)
- [Docker文档](https://docs.docker.com/)
- [Gunicorn配置](https://docs.gunicorn.org/)
- [MongoDB文档](https://docs.mongodb.com/)
- [Qdrant文档](https://qdrant.tech/documentation/)

---

## 🎉 总结

您现在拥有：

✅ **生产级Dockerfile** - 优化的多阶段构建
✅ **完整的docker-compose配置** - 包含所有必需服务
✅ **Flask REST API示例** - 可直接使用的API端点
✅ **Parlant SDK集成** - 完整的集成示例
✅ **一键启动脚本** - 自动化部署流程
✅ **详细文档** - 部署和故障排查指南
✅ **生产环境配置** - 安全和性能优化

立即开始：
```bash
./start-docker.sh
```

祝您使用愉快！🚀
