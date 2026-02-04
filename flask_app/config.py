# Flask应用配置文件

import os
from datetime import timedelta


class Config:
    """基础配置"""
    # Flask配置
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    JSON_AS_ASCII = False
    JSON_SORT_KEYS = False

    # Parlant配置
    PARLANT_SERVER_URL = os.getenv('PARLANT_SERVER_URL', 'http://localhost:8800')
    PARLANT_API_KEY = os.getenv('PARLANT_API_KEY', '')

    # LLM配置
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

    # 数据库配置
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        'sqlite:///parlant_flask.db'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', 'logs/flask_app.log')

    # CORS配置
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*').split(',')

    # 会话配置
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)


class DevelopmentConfig(Config):
    """开发环境配置"""
    DEBUG = True
    TESTING = False
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    """生产环境配置"""
    DEBUG = False
    TESTING = False

    # 生产环境必须设置SECRET_KEY
    SECRET_KEY = os.getenv('SECRET_KEY')
    if not SECRET_KEY:
        raise ValueError("SECRET_KEY must be set in production")


class TestingConfig(Config):
    """测试环境配置"""
    DEBUG = True
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


# 配置字典
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}


def get_config():
    """获取当前环境配置"""
    env = os.getenv('FLASK_ENV', 'development')
    return config.get(env, config['default'])
