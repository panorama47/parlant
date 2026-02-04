#!/bin/bash
# 验证 Parlant Docker 镜像中的依赖安装

echo "🔍 验证 Parlant 安装"
echo "===================="

# 检查 Python 版本
echo ""
echo "1. Python 版本:"
python --version

# 检查 Parlant 版本
echo ""
echo "2. Parlant 版本:"
python -c "import parlant; print(f'Parlant {parlant.__version__}')"

# 检查核心依赖
echo ""
echo "3. 核心依赖检查:"
python << 'EOF'
import sys

deps = {
    'fastapi': 'Web框架',
    'uvicorn': 'ASGI服务器',
    'openai': 'OpenAI客户端',
    'tiktoken': '分词器',
    'structlog': '日志',
    'rich': '终端美化',
    'httpx': 'HTTP客户端',
    'jinja2': '模板引擎',
    'jsonschema': 'JSON验证',
}

for module, desc in deps.items():
    try:
        mod = __import__(module)
        version = getattr(mod, '__version__', 'unknown')
        print(f"  ✓ {module:20s} {version:15s} ({desc})")
    except ImportError:
        print(f"  ✗ {module:20s} 未安装")
        sys.exit(1)
EOF

# 检查 Parlant 命令
echo ""
echo "4. Parlant 命令:"
which parlant-server && echo "  ✓ parlant-server 已安装"
which parlant && echo "  ✓ parlant 已安装"

# 检查 Flask
echo ""
echo "5. Flask 生态:"
python -c "import flask; print(f'  ✓ Flask {flask.__version__}')"
python -c "import flask_cors; print('  ✓ Flask-CORS 已安装')"
python -c "import gunicorn; print('  ✓ Gunicorn 已安装')"
python -c "import gevent; print('  ✓ Gevent 已安装')"

# 统计已安装包数量
echo ""
echo "6. 已安装包统计:"
TOTAL=$(pip list | wc -l)
echo "  总计: $((TOTAL - 2)) 个包"

# 列出所有 Parlant 相关包
echo ""
echo "7. Parlant 相关包:"
pip list | grep -i parlant

echo ""
echo "✅ 验证完成！"
