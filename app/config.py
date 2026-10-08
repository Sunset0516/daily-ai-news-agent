"""配置模块：从 .env 读取环境变量，提供全局 Settings 单例和路径常量。"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的父目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加载 .env（不存在不报错，方便首次部署）
load_dotenv(PROJECT_ROOT / ".env")

# 工具可访问的根目录白名单（路径越狱防护用）
ALLOWED_ROOTS = [PROJECT_ROOT, PROJECT_ROOT / "briefings"]

# 简报输出目录
BRIEFINGS_DIR = PROJECT_ROOT / "briefings"

# SQLite 数据库路径
DB_PATH = PROJECT_ROOT / "data.db"

# schema.sql 路径
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"


@dataclass
class Settings:
    """从环境变量读取的运行时配置。"""

    # LLM
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.3"))
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))

    # 搜索
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")

    # 调度
    briefing_hour: int = int(os.getenv("BRIEFING_HOUR", "8"))
    briefing_minute: int = int(os.getenv("BRIEFING_MINUTE", "0"))

    # 应用
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8000"))

    def validate(self) -> list:
        """返回缺失配置的提示列表（空表示全部就绪）。"""
        missing = []
        if not self.deepseek_api_key:
            missing.append("DEEPSEEK_API_KEY")
        if not self.tavily_api_key:
            missing.append("TAVILY_API_KEY")
        return missing


settings = Settings()
