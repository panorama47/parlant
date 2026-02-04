#!/bin/bash
# Parlant + Flask Docker快速启动脚本

set -e

echo "🚀 Parlant + Flask Docker 快速启动"
echo "=================================="

# 检查Docker是否安装
if ! command -v docker &> /dev/null; then
    echo "❌ 错误: Docker未安装"
    echo "请访问 https://docs.docker.com/get-docker/ 安装Docker"
    exit 1
fi

# 检查Docker Compose是否安装
if ! command -v docker-compose &> /dev/null; then
    echo "❌ 错误: Docker Compose未安装"
    echo "请访问 https://docs.docker.com/compose/install/ 安装Docker Compose"
    exit 1
fi

# 检查.env文件
if [ ! -f .env ]; then
    echo "📝 创建.env文件..."
    cp .env.example .env
    echo "⚠️  请编辑 .env 文件，填入你的API密钥"
    echo ""
    read -p "是否现在编辑.env文件? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ${EDITOR:-nano} .env
    else
        echo "请手动编辑.env文件后重新运行此脚本"
        exit 0
    fi
fi

# 检查OPENAI_API_KEY
source .env
if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "sk-your-openai-key-here" ]; then
    echo "❌ 错误: 请在.env文件中设置有效的OPENAI_API_KEY"
    exit 1
fi

# 创建必要的目录
echo "📁 创建目录..."
mkdir -p logs data flask_app

# 构建镜像
echo "🔨 构建Docker镜像..."
docker-compose -f docker-compose.yml build

# 启动服务
echo "🚀 启动服务..."
docker-compose -f docker-compose.yml up -d

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 10

# 检查服务状态
echo ""
echo "📊 服务状态:"
docker-compose -f docker-compose.yml ps

# 健康检查
echo ""
echo "🏥 健康检查:"

docker-compose -f docker-compose.yml exec -T parlant-server curl -f http://localhost:8800/health &> /dev/null
echo "✅ Parlant服务器: 运行正常 (http://localhost:8800)" || echo "❌ Parlant服务器: 未响应"

docker-compose -f docker-compose.yml exec -T flask-app curl -f http://localhost:5000/health &> /dev/null
echo "✅ Flask应用: 运行正常 (http://localhost:5000)" || echo "❌ Flask应用: 未响应"

echo ""
echo "✨ 启动完成!"
echo ""
echo "📚 可用服务:"
echo "  - Parlant服务器: http://localhost:8800"
echo "  - Flask API: http://localhost:5000"
echo "  - MongoDB: localhost:27017"
echo "  - Qdrant: http://localhost:6333"
echo ""
echo "📖 查看日志: docker-compose -f docker-compose.yml logs -f"
echo "🛑 停止服务: docker-compose -f docker-compose.yml down"
echo "🧹 清理数据: docker-compose -f docker-compose.yml down -v"
echo ""
