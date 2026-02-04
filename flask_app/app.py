"""
Flask应用示例 - 与Parlant集成
提供REST API接口来与Parlant智能体交互
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import asyncio
from typing import Optional
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# 配置
app.config['JSON_AS_ASCII'] = False
app.config['PARLANT_SERVER_URL'] = os.getenv('PARLANT_SERVER_URL', 'http://localhost:8800')

# Parlant客户端（延迟初始化）
parlant_client = None


def get_parlant_client():
    """获取或创建Parlant客户端"""
    global parlant_client
    if parlant_client is None:
        try:
            import parlant.sdk as p
            parlant_client = p
            logger.info("Parlant SDK initialized successfully")
        except ImportError:
            logger.error("Failed to import Parlant SDK")
            raise
    return parlant_client


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    return jsonify({
        'status': 'healthy',
        'service': 'parlant-flask-app',
        'version': '1.0.0'
    }), 200


@app.route('/api/agents', methods=['GET'])
def list_agents():
    """列出所有智能体"""
    try:
        # 这里应该调用Parlant API获取agents列表
        # 示例返回
        return jsonify({
            'agents': [
                {'id': 'agent-1', 'name': 'Customer Support Agent'},
                {'id': 'agent-2', 'name': 'Sales Agent'}
            ]
        }), 200
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """
    与智能体对话

    请求体:
    {
        "agent_id": "agent-1",
        "session_id": "session-123",
        "message": "用户消息",
        "customer_id": "customer-456"
    }
    """
    try:
        data = request.get_json()

        if not data or 'message' not in data:
            return jsonify({'error': 'Missing message in request'}), 400

        agent_id = data.get('agent_id', 'default-agent')
        session_id = data.get('session_id')
        message = data.get('message')
        customer_id = data.get('customer_id', 'anonymous')

        logger.info(f"Chat request - Agent: {agent_id}, Session: {session_id}, Customer: {customer_id}")

        # 这里应该调用Parlant API处理消息
        # 示例响应
        response = {
            'agent_id': agent_id,
            'session_id': session_id or 'new-session-id',
            'message': f"收到您的消息: {message}",
            'timestamp': '2026-01-28T12:00:00Z'
        }

        return jsonify(response), 200

    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id: str):
    """获取会话历史"""
    try:
        # 这里应该调用Parlant API获取会话历史
        return jsonify({
            'session_id': session_id,
            'messages': [
                {'role': 'user', 'content': '你好'},
                {'role': 'assistant', 'content': '您好！有什么可以帮您的吗？'}
            ]
        }), 200
    except Exception as e:
        logger.error(f"Error getting session: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/guidelines', methods=['POST'])
def create_guideline():
    """
    创建新的行为指南

    请求体:
    {
        "agent_id": "agent-1",
        "condition": "用户询问价格",
        "action": "提供详细的价格信息"
    }
    """
    try:
        data = request.get_json()

        if not data or 'condition' not in data or 'action' not in data:
            return jsonify({'error': 'Missing condition or action'}), 400

        agent_id = data.get('agent_id', 'default-agent')
        condition = data.get('condition')
        action = data.get('action')

        logger.info(f"Creating guideline for agent {agent_id}")

        # 这里应该调用Parlant API创建guideline
        guideline = {
            'id': 'guideline-123',
            'agent_id': agent_id,
            'condition': condition,
            'action': action,
            'created_at': '2026-01-28T12:00:00Z'
        }

        return jsonify(guideline), 201

    except Exception as e:
        logger.error(f"Error creating guideline: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/tools', methods=['GET'])
def list_tools():
    """列出可用的工具"""
    try:
        tools = [
            {
                'id': 'search_products',
                'name': 'Search Products',
                'description': '搜索产品信息'
            },
            {
                'id': 'check_inventory',
                'name': 'Check Inventory',
                'description': '检查库存状态'
            }
        ]
        return jsonify({'tools': tools}), 200
    except Exception as e:
        logger.error(f"Error listing tools: {e}")
        return jsonify({'error': str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    """404错误处理"""
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    """500错误处理"""
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    # 开发环境直接运行
    app.run(host='0.0.0.0', port=5000, debug=False)
