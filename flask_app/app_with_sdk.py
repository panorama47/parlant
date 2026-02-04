"""
Parlant + Flask 集成示例
展示如何在Flask应用中使用Parlant SDK
"""
import asyncio
from flask import Flask, request, jsonify
from flask_cors import CORS
import parlant.sdk as p
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# 全局变量存储server和agent
server = None
agent = None


async def initialize_parlant():
    """初始化Parlant服务器和智能体"""
    global server, agent

    try:
        # 启动Parlant服务器
        server = await p.Server().__aenter__()
        logger.info("Parlant server started")

        # 创建智能体
        agent = await server.create_agent(
            name="Customer Support Agent",
            description="A helpful customer support agent"
        )
        logger.info(f"Agent created: {agent.name}")

        # 创建示例guideline
        await agent.create_guideline(
            condition="用户询问产品信息",
            action="提供详细的产品信息，包括价格、特性和可用性"
        )

        await agent.create_guideline(
            condition="用户需要技术支持",
            action="询问具体问题，提供逐步解决方案"
        )

        logger.info("Guidelines created")

        return True

    except Exception as e:
        logger.error(f"Failed to initialize Parlant: {e}")
        return False


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        'status': 'healthy',
        'parlant_initialized': server is not None and agent is not None
    }), 200


@app.route('/api/chat', methods=['POST'])
def chat():
    """
    与智能体对话

    请求体:
    {
        "message": "用户消息",
        "session_id": "可选的会话ID"
    }
    """
    if not agent:
        return jsonify({'error': 'Parlant not initialized'}), 503

    try:
        data = request.get_json()
        message = data.get('message')
        session_id = data.get('session_id')

        if not message:
            return jsonify({'error': 'Message is required'}), 400

        # 使用asyncio运行异步代码
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def process_message():
            # 创建或获取会话
            if session_id:
                session = await agent.get_session(session_id)
            else:
                session = await agent.create_session()

            # 发送消息
            await session.send(message)

            # 获取响应
            events = await session.list_events()

            # 提取最后的AI响应
            ai_messages = [
                e.data.get('message', '')
                for e in events
                if e.kind == 'message' and e.source == 'ai_agent'
            ]

            return {
                'session_id': session.id,
                'response': ai_messages[-1] if ai_messages else "No response",
                'message_count': len(events)
            }

        result = loop.run_until_complete(process_message())
        loop.close()

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error in chat: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/guidelines', methods=['GET'])
def list_guidelines():
    """列出所有guidelines"""
    if not agent:
        return jsonify({'error': 'Parlant not initialized'}), 503

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def get_guidelines():
            guidelines = await agent.list_guidelines()
            return [
                {
                    'id': g.id,
                    'condition': g.content.condition,
                    'action': g.content.action
                }
                for g in guidelines
            ]

        result = loop.run_until_complete(get_guidelines())
        loop.close()

        return jsonify({'guidelines': result}), 200

    except Exception as e:
        logger.error(f"Error listing guidelines: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/guidelines', methods=['POST'])
def create_guideline():
    """
    创建新的guideline

    请求体:
    {
        "condition": "条件描述",
        "action": "动作描述"
    }
    """
    if not agent:
        return jsonify({'error': 'Parlant not initialized'}), 503

    try:
        data = request.get_json()
        condition = data.get('condition')
        action = data.get('action')

        if not condition or not action:
            return jsonify({'error': 'Condition and action are required'}), 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def add_guideline():
            guideline = await agent.create_guideline(
                condition=condition,
                action=action
            )
            return {
                'id': guideline.id,
                'condition': guideline.content.condition,
                'action': guideline.content.action
            }

        result = loop.run_until_complete(add_guideline())
        loop.close()

        return jsonify(result), 201

    except Exception as e:
        logger.error(f"Error creating guideline: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # 初始化Parlant
    loop = asyncio.get_event_loop()
    success = loop.run_until_complete(initialize_parlant())

    if not success:
        logger.error("Failed to initialize Parlant, exiting...")
        exit(1)

    # 启动Flask应用
    app.run(host='0.0.0.0', port=5000, debug=False)
