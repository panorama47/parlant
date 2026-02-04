# Parlant + Flask Docker 快速参考

## 🚀 一键启动
```bash
./start-docker.sh
```

## 📋 常用命令

### 启动和停止
```bash
# 启动所有服务
docker-compose up -d

# 停止所有服务
docker-compose down

# 重启服务
docker-compose restart

# 停止并删除数据
docker-compose down -v
```

### 查看状态
```bash
# 查看运行状态
docker-compose ps

# 查看日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f flask-app
docker-compose logs -f parlant-server
```

### 进入容器
```bash
# 进入Flask容器
docker-compose exec flask-app bash

# 进入Parlant容器
docker-compose exec parlant-server bash

# 进入MongoDB
docker-compose exec mongodb mongosh
```

### 测试
```bash
# 运行测试脚本
./test-docker.sh

# 手动测试API
curl http://localhost:5000/health
curl http://localhost:5000/api/agents
```

## 🔧 配置文件

| 文件 | 说明 |
|------|------|
| `.env` | 环境变量配置 |
| `docker-compose.yml` | 服务编排 |
| `Dockerfile` | Parlant基础镜像 |
| `Dockerfile.flask` | Flask应用镜像 |

## 🌐 服务端口

| 服务 | 端口 | URL |
|------|------|-----|
| Flask应用 | 5000 | http://localhost:5000 |
| Parlant服务器 | 8800 | http://localhost:8800 |
| MongoDB | 27017 | mongodb://localhost:27017 |
| Qdrant | 6333 | http://localhost:6333 |

## 📝 API端点

### GET /health
健康检查

### POST /api/chat
```json
{
  "message": "你好",
  "agent_id": "agent-1",
  "customer_id": "user-123"
}
```

### GET /api/agents
列出所有智能体

### POST /api/guidelines
```json
{
  "condition": "用户询问价格",
  "action": "提供详细价格信息"
}
```

## 🐛 故障排查

### 容器无法启动
```bash
docker-compose logs <service-name>
docker-compose build --no-cache
```

### 端口冲突
编辑 `docker-compose.yml` 修改端口映射

### 数据库连接失败
```bash
docker-compose ps mongodb
docker-compose restart mongodb
```

### 清理重建
```bash
docker-compose down -v
docker-compose build --no-cache
docker-compose up -d
```

## 📚 文档

- 详细指南: `DOCKER_GUIDE.md`
- 完整总结: `DOCKER_SUMMARY.md`
- 项目说明: `README.md`

## 🆘 获取帮助

- Discord: https://discord.gg/duxWqxKk6J
- 文档: https://parlant.io/docs
- GitHub: https://github.com/emcie-co/parlant
