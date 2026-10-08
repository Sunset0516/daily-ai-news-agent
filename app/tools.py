"""Agent 工具集：7 个工具 + 路径/命令安全约束。

设计原则：
- 所有工具失败不抛异常，返回 "ERROR: ..." 字符串作为 Observation，
  让 LLM 感知错误并自主决策（容错核心，见 push_to_feishu）。
- 路径类工具限制在 ALLOWED_ROOTS 白名单内，防越狱。
- write_file 仅允许写 briefings/ 子树，防覆盖源码。
- bash 用 denylist 正则阻断危险命令，超时 10s。
- search_web 用 Tavily，是新闻唯一合法来源。
- push_to_feishu 失败返回 ERROR，由 LLM 决定重试或回退本地文件。
"""
import re
import subprocess
from pathlib import Path

import requests
from langchain_core.tools import tool
from tavily import TavilyClient

from app.config import ALLOWED_ROOTS, BRIEFINGS_DIR, PROJECT_ROOT, settings


# ---------- 路径安全 ----------

def _resolve_in_roots(path: str):
    """解析路径，返回 (resolved_path, error_str)。若越界返回 (None, 错误说明)。"""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p = p.resolve()
    except (OSError, ValueError) as e:
        return None, f"ERROR: invalid path: {e}"
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p, None
        except ValueError:
            continue
    return None, f"ERROR: path out of allowed scope: {path}"


# ---------- 工具实现 ----------

@tool
def list_dir(path: str) -> str:
    """列出目录内容。path 可以是相对或绝对路径。返回每行一个条目，目录后加 /。"""
    p, err = _resolve_in_roots(path)
    if err:
        return err
    if not p.exists():
        return f"ERROR: not found: {path}"
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    items = []
    for child in sorted(p.iterdir()):
        items.append(f"{child.name}/" if child.is_dir() else child.name)
    return "\n".join(items) if items else "(empty)"


@tool
def read_file(path: str) -> str:
    """读取文本文件内容。path 可以是相对或绝对路径。文件大于 1MB 会截断。"""
    p, err = _resolve_in_roots(path)
    if err:
        return err
    if not p.exists():
        return f"ERROR: not found: {path}"
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    try:
        size = p.stat().st_size
        if size > 1_000_000:
            text = p.read_text(encoding="utf-8", errors="replace")[:1_000_000]
            return text + f"\n... (truncated, original size {size} bytes)"
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: read failed: {e}"


@tool
def search_content(keyword: str, dir: str) -> str:
    """在目录下按关键词搜索文件内容。返回 '文件:行号:内容'，最多 50 条。"""
    p, err = _resolve_in_roots(dir)
    if err:
        return err
    if not p.exists() or not p.is_dir():
        return f"ERROR: dir not found: {dir}"
    matches = []
    for f in p.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix in {".db", ".exe", ".dll", ".png", ".jpg", ".zip", ".pyc"}:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if keyword in line:
                matches.append(f"{f}: {i}: {line.strip()[:200]}")
                if len(matches) >= 50:
                    return "\n".join(matches)
    return "\n".join(matches) if matches else "(no matches)"


@tool
def write_file(path: str, content: str) -> str:
    """写入文本文件。path 必须以 'briefings/' 开头（仅允许写简报目录），防止越狱写源码。"""
    norm = path.replace("\\", "/")
    if not (norm.startswith("briefings/") or norm.startswith("./briefings/")):
        return "ERROR: write_file path must start with 'briefings/'"
    src = Path(path)
    p = src.resolve() if src.is_absolute() else (PROJECT_ROOT / path).resolve()
    # 解析后必须仍在 briefings 子树内，防 ../ 越狱
    try:
        p.relative_to(BRIEFINGS_DIR.resolve())
    except ValueError:
        return f"ERROR: path escapes briefings/: {path}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {path} ({len(content.encode('utf-8'))} bytes)"
    except Exception as e:
        return f"ERROR: write failed: {e}"


# bash 危险命令黑名单（大小写不敏感）
_BASH_DENY = re.compile(
    r"(rm\s+-rf|del\s+/[fsq]|format\s+[a-z]:|mkfs|sudo\s|"
    r"\bcurl\b|\bwget\b|\bnc\b|\bncat\b|"
    r">\s*/|>>\s*/|"
    r":\(\)\s*\{|fork\s*bomb|"
    r"shutdown|reboot|halt|poweroff|"
    r"reg\s+add|regedit|"
    r"taskkill\s+/f)",
    re.IGNORECASE,
)


@tool
def bash(command: str) -> str:
    """执行受限的 shell 命令。工作目录锁定在项目根。危险命令（rm -rf、curl、wget、shutdown 等）会被拒绝。超时 10 秒。注意：搜索新闻只能用 search_web，不能用 bash curl。"""
    if _BASH_DENY.search(command):
        return f"ERROR: command blocked by denylist: {command[:100]}"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (result.stdout or "") + (result.stderr or "")
        out = out[:8000]
        return f"exit_code={result.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out (10s)"
    except Exception as e:
        return f"ERROR: exec failed: {e}"


@tool
def search_web(query: str) -> str:
    """用 Tavily 搜索全网新闻/网页。返回 top 5 结果（标题/链接/摘要）。这是获取新闻的唯一合法来源，不要凭记忆编造新闻。"""
    if not settings.tavily_api_key:
        return "ERROR: TAVILY_API_KEY not configured"
    try:
        client = TavilyClient(api_key=settings.tavily_api_key)
        resp = client.search(query=query, max_results=5, search_depth="advanced")
        results = resp.get("results", [])
        if not results:
            return f"(no results for: {query})"
        lines = []
        for r in results:
            lines.append(
                f"- title: {r.get('title', '')}\n"
                f"  url: {r.get('url', '')}\n"
                f"  content: {(r.get('content', '') or '')[:300]}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"ERROR: search failed: {e}"


@tool
def push_to_feishu(webhook_url: str, content: str) -> str:
    """推送消息到飞书自定义机器人 webhook。返回 'OK: pushed' 或 'ERROR: <原因>'。失败时你应当感知错误并决定：重试一次，或回退到只保留本地文件。最多重试 1 次后请放弃推送。"""
    if not webhook_url:
        return "ERROR: webhook_url is empty"
    # 飞书文本消息长度限制，超 4000 字符截断
    payload_text = content
    if len(content) > 4000:
        payload_text = content[:3800] + "\n\n...(内容过长已截断，完整内容见本地文件)"
    payload = {
        "msg_type": "text",
        "content": {"text": payload_text},
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=8)
        if resp.status_code != 200:
            return f"ERROR: HTTP {resp.status_code}, consider retry once or fall back to local file"
        data = resp.json()
        # 飞书成功响应 StatusCode=0 或 code=0
        if data.get("StatusCode", 0) != 0 and data.get("code", 0) != 0:
            return f"ERROR: feishu rejected: {data}"
        return "OK: pushed"
    except requests.Timeout:
        return "ERROR: timeout (8s), consider retry once or fall back to local file"
    except Exception as e:
        return f"ERROR: push failed: {e}, consider fall back to local file"


# 工具清单（供 agent 构建时注入）
ALL_TOOLS = [list_dir, read_file, search_content, write_file, bash, search_web, push_to_feishu]
