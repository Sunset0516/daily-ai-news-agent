"""推送/渲染辅助模块。

飞书推送逻辑在 tools.push_to_feishu（agent 工具，失败返回 ERROR 让 LLM 感知）。
本模块负责把简报记录转成前端友好结构，以及提供飞书消息文本格式化辅助。
"""
from typing import Optional


def render_for_frontend(briefing: Optional[dict]) -> dict:
    """把简报记录转成前端友好结构（详情接口用）。"""
    if not briefing:
        return {}
    return {
        "briefing_id": briefing.get("briefing_id"),
        "user_id": briefing.get("user_id"),
        "briefing_date": briefing.get("briefing_date"),
        "title": briefing.get("title", ""),
        "summary": briefing.get("summary", ""),
        "markdown_content": briefing.get("markdown_content", ""),
        "file_path": briefing.get("file_path", ""),
        "feishu_pushed": briefing.get("feishu_pushed", 0),
        "push_error": briefing.get("push_error"),
        "tool_call_count": briefing.get("tool_call_count", 0),
        "agent_log": briefing.get("agent_log", []),
        "created_at": briefing.get("created_at"),
    }


def format_briefing_for_feishu(markdown: str, title: str) -> str:
    """把简报 markdown 格式化成飞书文本消息。

    飞书文本消息不渲染 markdown，这里加标题强调 + 截断到 3800 字符。
    agent 通常直接传 markdown 给 push_to_feishu 工具，本函数供外部补推时用。
    """
    if not markdown:
        return title
    text = markdown[:3800]
    return f"📋 {title}\n\n{text}"
