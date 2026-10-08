"""ReAct Agent 核心：用 LangGraph create_react_agent + DeepSeek 构建。

核心要求①：工具调用顺序完全由 LLM 决策，非硬编码线性流程。

提供两个入口：
- run_briefing_stream(user, preferences): async 生成器，yield 中间步骤事件（供 SSE 前端实时展示思考过程）
- run_briefing_sync(user, preferences): 同步运行，返回 (briefing, agent_log)（定时任务用，思考过程写入 DB）

容错：push_to_feishu 失败时，LLM 通过 Observation 感知 ERROR 并自主决定重试或回退本地文件。
"""
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.config import BRIEFINGS_DIR, PROJECT_ROOT, settings
from app.db import get_briefing_by_user_date, save_briefing
from app.tools import ALL_TOOLS

SYSTEM_PROMPT = """你是一个"每日 AI 新闻助手"Agent。根据用户订阅偏好，搜集当天 AI 新闻，整理成 markdown 简报，写入本地文件，并尝试推送到飞书，然后结束。

你可以使用以下工具（由系统注入，无需你声明）：
- list_dir(path): 列出目录
- read_file(path): 读取文本文件
- search_content(keyword, dir): 按关键词搜索目录下文件内容
- write_file(path, content): 写入文件（仅限 briefings/ 目录）
- bash(command): 执行受限 shell 命令（工作目录锁定项目根）
- search_web(query): 用 Tavily 搜索全网新闻（必须用它获取新闻，不能凭记忆编造）
- push_to_feishu(webhook_url, content): 推送消息到飞书自定义机器人。返回 "OK: pushed" 或 "ERROR: <原因>"

工作流程（由你自主决定调用顺序与次数，以下是建议非硬编码）：
1. 阅读用户订阅偏好中的关键词和话题。
2. 对每个关键词调用 search_web 搜索当天新闻（可多次）。
3. 必要时 read_file 查看历史简报避免重复，或 list_dir 浏览 briefings/。
4. 综合搜索结果，去重、按重要性排序，挑选 5-10 条。
5. 调用 write_file 写入 briefings/YYYY-MM-DD.md。
6. 调用 push_to_feishu 推送简报到用户 webhook。观察返回：
   - 如果返回 "OK: pushed"，结束，输出 "FINISH: pushed"。
   - 如果返回 "ERROR: ..."，可以重试一次（最多 1 次）。重试仍失败，则不再推送，仅保留本地文件，
     输出 "FINISH: local only, push failed: <原因>"，让外部记录 feishu_pushed=0 以便后续补推。

硬约束：
- 不得凭记忆编造新闻，必须有 search_web 的结果作依据。
- 每条新闻必须包含原文链接。
- write_file 的 path 必须以 "briefings/" 开头。
- bash 不得执行删除/格式化/网络下载等危险操作；搜索新闻只能用 search_web，不能 bash curl。
- search_web 失败或无结果时直接说明原因，不要写空简报。
- push_to_feishu 失败时不要无限重试，最多重试 1 次后回退到本地文件方案。

简报 markdown 格式（严格遵守）：
# 每日 AI 新闻简报 - {YYYY-MM-DD}
> 生成时间: {timestamp}
> 订阅关键词: {逗号分隔偏好}

## 概述
{2-3 句总结当天 AI 整体动向}

## 新闻条目
### 1. {新闻标题}
- 链接: {原文 URL}
- 摘要: {1-2 句中文摘要}
- 关联偏好: {命中的关键词}
（5-10 条）

## 结尾
- 生成工具: DeepSeek + LangChain ReAct agent
- 工具调用次数: {N}
- 飞书推送状态: {success | failed: 原因}
"""


def _build_llm() -> ChatOpenAI:
    """构建 DeepSeek LLM（OpenAI 兼容协议）。"""
    return ChatOpenAI(
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )


def _build_agent():
    """构建 ReAct agent。工具调用顺序由 LLM 决策。"""
    llm = _build_llm()
    return create_react_agent(llm, ALL_TOOLS, prompt=SYSTEM_PROMPT)


def _build_input(user: dict, preferences: list) -> dict:
    """根据用户和偏好构造 agent 输入消息。"""
    today = datetime.now().strftime("%Y-%m-%d")
    prefs = [f"{p['type']}={p['value']}" for p in preferences]
    prefs_str = ", ".join(prefs) if prefs else "(无偏好，请用通用 AI 关键词如 LLM、Agent 搜索)"
    webhook = user.get("feishu_webhook") or "(未配置飞书 webhook，跳过推送，仅写本地文件)"
    msg = (
        f"今天是 {today}。请为用户「{user['name']}」生成今日 AI 新闻简报。\n"
        f"订阅偏好：{prefs_str}\n"
        f"飞书 webhook：{webhook}\n"
        f"请按系统提示词的格式输出简报，写入 briefings/{today}.md，"
        f"并尝试推送到飞书。最后输出 FINISH 行。"
    )
    return {"messages": [HumanMessage(content=msg)]}


def _parse_finish(text: str) -> tuple:
    """从 agent 最终输出解析推送状态。返回 (feishu_pushed, push_error)。"""
    if not text:
        return 0, "no final output"
    low = text.lower()
    if "local only" in low or "push failed" in low or "push_error" in low:
        m = re.search(r"push failed[:\s]*(.+?)(\n|$)", text, re.IGNORECASE)
        err = m.group(1).strip() if m else "push failed"
        return 0, err[:200]
    if "pushed" in low:
        return 1, None
    return 0, "unknown finish status"


def _read_briefing_file(file_path: str) -> str:
    """从 briefings/ 读回最终简报 markdown（安全：路径限制在 briefings 子树）。"""
    try:
        p = (PROJECT_ROOT / file_path).resolve()
        p.relative_to(BRIEFINGS_DIR.resolve())
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"(读取简报文件失败: {e})"


def _extract_briefing_fields(final_text: str, user: dict, file_path: str,
                             tool_call_count: int, feishu_pushed: int,
                             push_error: Optional[str]) -> dict:
    """从最终输出提取简报字段用于 DB 存储。"""
    today = datetime.now().strftime("%Y-%m-%d")
    title = f"每日 AI 新闻简报 - {today}"
    summary = ""
    m = re.search(r"## 概述\s*\n(.+?)(?=\n##|\Z)", final_text, re.DOTALL)
    if m:
        summary = m.group(1).strip()[:300]
    return {
        "user_id": user["user_id"],
        "briefing_date": today,
        "title": title,
        "summary": summary,
        "markdown_content": final_text,
        "file_path": file_path,
        "feishu_pushed": feishu_pushed,
        "push_error": push_error,
        "tool_call_count": tool_call_count,
    }


def _process_message(msg, step: int, tool_call_count: int, file_path: str):
    """处理单条消息，返回 (events, new_step, new_tool_call_count, new_file_path, is_final)。

    事件结构：
    - thought: {step, type:"thought", content}
    - action: {step, type:"action", tool, input}
    - observation: {step, type:"observation", tool, content}
    """
    events = []
    is_final = False
    if isinstance(msg, AIMessage):
        if msg.content:
            step += 1
            events.append({"step": step, "type": "thought", "content": str(msg.content)[:2000]})
        if msg.tool_calls:
            for tc in msg.tool_calls:
                step += 1
                tool_call_count += 1
                events.append({
                    "step": step, "type": "action",
                    "tool": tc.get("name", ""),
                    "input": json.dumps(tc.get("args", {}), ensure_ascii=False),
                })
        else:
            # AIMessage 无 tool_calls 且有 content → 最终回复
            is_final = bool(msg.content)
    elif isinstance(msg, ToolMessage):
        step += 1
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        events.append({
            "step": step, "type": "observation",
            "tool": msg.name, "content": content[:2000],
        })
        if msg.name == "write_file" and "OK: wrote" in str(content):
            m = re.search(r"wrote (\S+)", content)
            if m:
                file_path = m.group(1)
    return events, step, tool_call_count, file_path, is_final


async def run_briefing_stream(user: dict, preferences: list) -> AsyncGenerator:
    """流式运行 agent，yield 中间步骤事件（供 SSE 前端实时展示思考过程）。

    finish 事件包含 briefing_id/file_path/feishu_pushed/push_error/tool_call_count。
    """
    agent = _build_agent()
    inputs = _build_input(user, preferences)
    agent_log = []
    step = 0
    tool_call_count = 0
    final_text = ""
    file_path = ""

    async for chunk in agent.astream(inputs, stream_mode="values"):
        messages = chunk.get("messages", [])
        for msg in messages:
            events, step, tool_call_count, file_path, is_final = _process_message(
                msg, step, tool_call_count, file_path)
            for ev in events:
                agent_log.append(ev)
                yield ev
            if is_final and msg.content:
                final_text = str(msg.content)

    # 解析推送状态
    feishu_pushed, push_error = _parse_finish(final_text)
    if not final_text and file_path:
        final_text = _read_briefing_file(file_path)

    fields = _extract_briefing_fields(
        final_text, user, file_path, tool_call_count, feishu_pushed, push_error)
    briefing = save_briefing(agent_log=agent_log, **fields)

    yield {
        "step": step + 1, "type": "finish",
        "briefing_id": briefing["briefing_id"] if briefing else None,
        "file_path": file_path,
        "feishu_pushed": feishu_pushed,
        "push_error": push_error,
        "tool_call_count": tool_call_count,
    }


def run_briefing_sync(user: dict, preferences: list) -> tuple:
    """同步运行 agent（定时任务用）。返回 (briefing, agent_log)。

    内部用 asyncio.run 跑 stream 版本，思考过程写入 DB agent_log 字段供回看。
    """
    agent_log = []

    async def _collect():
        async for event in run_briefing_stream(user, preferences):
            agent_log.append(event)

    try:
        asyncio.run(_collect())
    except RuntimeError:
        # 已有 event loop（不应发生在 BackgroundScheduler 线程）
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_collect())
        finally:
            loop.close()

    briefing = get_briefing_by_user_date(
        user["user_id"], datetime.now().strftime("%Y-%m-%d"))
    return briefing, agent_log
