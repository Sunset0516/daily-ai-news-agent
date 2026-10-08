"""agent.py 纯函数单元测试：_parse_finish / _read_briefing_file / _extract_briefing_fields。

不测：_build_agent（要真 LLM 连接）、run_briefing_stream（集成测试）。
"""
import os
from pathlib import Path

import pytest

os.environ.pop("TAVILY_API_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)

from app.agent import _parse_finish, _read_briefing_file, _extract_briefing_fields
from app.config import BRIEFINGS_DIR, PROJECT_ROOT


# ---------- _parse_finish 各分支 ----------

def test_parse_finish_pushed():
    assert _parse_finish("FINISH: pushed") == (1, None)


def test_parse_finish_pushed_lowercase():
    assert _parse_finish("finish: pushed") == (1, None)


def test_parse_finish_local_only_with_reason():
    assert _parse_finish("FINISH: local only, push failed: HTTP 405") == (0, "HTTP 405")


def test_parse_finish_local_only_skipped():
    assert _parse_finish("FINISH: local only, push skipped: no webhook") == (0, "no webhook")


def test_parse_finish_local_only_no_reason():
    assert _parse_finish("FINISH: local only") == (0, "local only (no webhook or push failed)")


def test_parse_finish_empty():
    assert _parse_finish("") == (0, "no final output")


def test_parse_finish_not_pushed():
    """含 pushed 但有 not pushed 应判 0。"""
    assert _parse_finish("FINISH: not pushed") == (0, "unknown finish status")


def test_parse_finish_unknown():
    assert _parse_finish("hello world") == (0, "unknown finish status")


def test_parse_finish_push_error_variant():
    """push error 后跟原因。"""
    assert _parse_finish("FINISH: local only, push error: timeout") == (0, "timeout")


# ---------- _read_briefing_file 路径安全 ----------

def test_read_briefing_file_ok():
    """正常 briefings/ 文件应能读取。"""
    test_file = BRIEFINGS_DIR / "_test_read.md"
    test_file.write_text("# test", encoding="utf-8")
    try:
        content = _read_briefing_file("briefings/_test_read.md")
        assert content == "# test"
    finally:
        test_file.unlink()


def test_read_briefing_file_escape_rejected():
    """越狱路径应返回错误字符串。"""
    content = _read_briefing_file("app/main.py")
    assert content.startswith("(读取简报文件失败")


def test_read_briefing_file_not_exist():
    content = _read_briefing_file("briefings/_nonexistent.md")
    assert content.startswith("(读取简报文件失败")


# ---------- _extract_briefing_fields ----------

def test_extract_briefing_fields_summary_extraction():
    """## 概述 节应被提取到 summary。"""
    md = """# 每日 AI 新闻简报

## 概述
今天有 3 条重要新闻，涉及大模型和开源。

## 新闻
1. 新闻一
"""
    user = {"user_id": 1, "name": "test", "feishu_webhook": None}
    fields = _extract_briefing_fields(
        md, user, file_path="briefings/2026-10-08.md",
        tool_call_count=5, feishu_pushed=0,
        push_error="HTTP 405")
    assert fields["user_id"] == 1
    assert fields["file_path"] == "briefings/2026-10-08.md"
    assert fields["feishu_pushed"] == 0
    assert fields["push_error"] == "HTTP 405"
    assert fields["tool_call_count"] == 5
    assert "3 条重要新闻" in fields["summary"]
    assert fields["markdown_content"] == md


def test_extract_briefing_fields_no_summary():
    """无 ## 概述 节时 summary 为空。"""
    md = "# 简报\n\n只有正文，没有概述节"
    user = {"user_id": 2, "name": "test2", "feishu_webhook": None}
    fields = _extract_briefing_fields(
        md, user, file_path="", tool_call_count=0,
        feishu_pushed=0, push_error=None)
    assert fields["summary"] == ""
    assert fields["markdown_content"] == md


def test_extract_briefing_fields_summary_truncation():
    """超长 summary 应截断到 300 字符。"""
    long_text = "x" * 500
    md = f"## 概述\n{long_text}"
    user = {"user_id": 1, "name": "t", "feishu_webhook": None}
    fields = _extract_briefing_fields(
        md, user, file_path="", tool_call_count=0,
        feishu_pushed=0, push_error=None)
    assert len(fields["summary"]) == 300
