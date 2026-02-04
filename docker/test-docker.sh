#!/bin/bash
# 测试Docker部署的脚本

echo "🧪 测试Parlant + Flask Docker部署"
echo "=================================="

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 测试函数
test_endpoint() {
    local url=$1
    local name=$2

    if curl -f -s "$url" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} $name: OK"
        return 0
    else
        echo -e "${RED}✗${NC} $name: FAILED"
        return 1
    fi
}

# 检查服务是否运行
echo ""
echo "1. 检查Docker容器状态..."
docker-compose -f docker-compose.yml ps

# 测试健康检查端点
echo ""
echo "2. 测试健康检查端点..."
test_endpoint "http://localhost:5000/health" "Flask应用健康检查"
test_endpoint "http://localhost:8800/health" "Parlant服务器健康检查"

# 测试API端点
echo ""
echo "3. 测试API端点..."
test_endpoint "http://localhost:5000/api/agents" "列出智能体"
test_endpoint "http://localhost:5000/api/tools" "列出工具"

# 测试聊天功能
echo ""
echo "4. 测试聊天功能..."
response=$(curl -s -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "customer_id": "test-user"}')

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} 聊天API: OK"
    echo "响应: $response"
else
    echo -e "${RED}✗${NC} 聊天API: FAILED"
fi

# 测试数据库连接
echo ""
echo "5. 测试数据库连接..."
if docker-compose -f docker-compose.yml exec -T mongodb mongosh --eval "db.adminCommand('ping')" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} MongoDB: OK"
else
    echo -e "${YELLOW}⚠${NC} MongoDB: 未运行或无法连接"
fi

# 测试Qdrant
echo ""
echo "6. 测试向量数据库..."
if curl -f -s "http://localhost:6333/collections" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Qdrant: OK"
else
    echo -e "${YELLOW}⚠${NC} Qdrant: 未运行或无法连接"
fi

# 检查日志
echo ""
echo "7. 检查最近的日志..."
echo "Flask应用日志:"
docker-compose -f docker-compose.yml logs --tail=5 flask-app

echo ""
echo "Parlant服务器日志:"
docker-compose -f docker-compose.yml logs --tail=5 parlant-server

echo ""
echo "=================================="
echo "✨ 测试完成!"
echo ""
