#!/bin/bash
# 验证Docker目录结构调整后的构建测试

echo "🔍 验证Docker目录结构调整"
echo "========================="

# 检查必要的文件是否存在
echo ""
echo "1. 检查文件结构..."

FILES_TO_CHECK=(
    "../pyproject.toml"
    "../src/parlant/__init__.py"
    "../README.md"
    "../scripts/"
    "Dockerfile"
    "Dockerfile.flask"
    "docker-compose.yml"
    "start-docker.sh"
    "test-docker.sh"
)

for file in "${FILES_TO_CHECK[@]}"; do
    if [ -e "$file" ]; then
        echo "✅ $file 存在"
    else
        echo "❌ $file 不存在"
    fi
done

# 测试Dockerfile语法
echo ""
echo "2. 验证Dockerfile语法..."
docker run --rm -v "$(pwd)/../:/workspace" -w /workspace/docker alpine:latest \
    sh -c "apk add --no-cache docker-cli && docker build --no-cache -t test-parlant-build . 2>&1 | head -20"

echo ""
echo "3. 显示目录结构..."
find . -name "*.sh" -o -name "Dockerfile*" -o -name "*.yml" | sort

echo ""
echo "✅ 验证完成!"
echo "💡 如需实际构建，请运行: ./start-docker.sh"