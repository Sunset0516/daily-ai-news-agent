"""FastAPI 应用入口：路由 + SSE 流式生成 + lifespan 启停调度器。

路由清单见 plan 第 3 节。核心是 POST /api/users/{id}/briefings/generate 用
StreamingResponse + agent.run_briefing_stream 实时推送 Thought/Action/Observation/Finish。
"""
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import run_briefing_stream
from app.config import PROJECT_ROOT, settings
from app.db import (
    add_preference,
    create_user,
    delete_preference,
    delete_user,
    get_briefing,
    get_user,
    init_db,
    list_briefings,
    list_preferences,
    list_users,
    update_user,
)
from app.push import render_for_frontend
from app.scheduler import (
    daily_briefing_job,
    get_scheduler_status,
    shutdown_scheduler,
    start_scheduler,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：startup 初始化 DB + 启动调度器；shutdown 关闭调度器。"""
    init_db()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="每日 AI 新闻助手", lifespan=lifespan)

# 静态文件目录（不存在则创建，避免挂载失败）
STATIC_DIR = PROJECT_ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- Pydantic 模型 ----------

class UserCreate(BaseModel):
    name: str
    feishu_webhook: str | None = None


class UserUpdate(BaseModel):
    name: str | None = None
    feishu_webhook: str | None = None


class PrefCreate(BaseModel):
    type: str  # 'topic' or 'keyword'
    value: str


# ---------- 基础路由 ----------

@app.get("/")
async def index():
    """返回前端首页。"""
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return {"message": "static/index.html not built yet, see /api/health"}
    return FileResponse(str(f))


@app.get("/api/health")
async def health():
    """健康检查 + 配置就绪状态。"""
    missing = settings.validate()
    return {
        "status": "ok" if not missing else "degraded",
        "missing": missing,
        "deepseek_model": settings.deepseek_model,
        "briefing_time": f"{settings.briefing_hour:02d}:{settings.briefing_minute:02d}",
    }


# ---------- 用户 ----------

@app.post("/api/users")
async def create_user_api(payload: UserCreate):
    try:
        return create_user(payload.name, payload.feishu_webhook)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/users")
async def list_users_api():
    return list_users()


@app.get("/api/users/{user_id}")
async def get_user_api(user_id: int):
    u = get_user(user_id)
    if not u:
        raise HTTPException(404, "user not found")
    return u


@app.put("/api/users/{user_id}")
async def update_user_api(user_id: int, payload: UserUpdate):
    u = update_user(user_id, payload.name, payload.feishu_webhook)
    if not u:
        raise HTTPException(404, "user not found")
    return u


@app.delete("/api/users/{user_id}")
async def delete_user_api(user_id: int):
    delete_user(user_id)
    return {"ok": True}


# ---------- 偏好 ----------

@app.post("/api/users/{user_id}/preferences")
async def add_pref_api(user_id: int, payload: PrefCreate):
    if payload.type not in ("topic", "keyword"):
        raise HTTPException(400, "type must be 'topic' or 'keyword'")
    if not get_user(user_id):
        raise HTTPException(404, "user not found")
    try:
        return add_preference(user_id, payload.type, payload.value)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/users/{user_id}/preferences")
async def list_prefs_api(user_id: int):
    return list_preferences(user_id)


@app.delete("/api/preferences/{pref_id}")
async def delete_pref_api(pref_id: int):
    delete_preference(pref_id)
    return {"ok": True}


# ---------- 简报 ----------

@app.post("/api/users/{user_id}/briefings/generate")
async def generate_briefing_api(user_id: int):
    """SSE 流式生成简报，实时推送 Thought/Action/Observation/Finish 事件。

    事件结构：{step, type:"thought"|"action"|"observation"|"finish", ...}
    前端用 EventSource 接收，思考过程卡片流式追加。
    """
    user = get_user(user_id)
    if not user:
        raise HTTPException(404, "user not found")
    prefs = list_preferences(user_id)
    if not prefs:
        raise HTTPException(400, "no preferences, please add topics/keywords first")

    async def event_gen():
        try:
            async for event in run_briefing_stream(user, prefs):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = {"type": "error", "content": f"{type(e).__name__}: {e}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/users/{user_id}/briefings")
async def list_briefings_api(user_id: int):
    return list_briefings(user_id)


@app.get("/api/briefings/{briefing_id}")
async def get_briefing_api(briefing_id: int):
    b = get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "briefing not found")
    return render_for_frontend(b)


# ---------- 调度 ----------

@app.get("/api/scheduler/status")
async def scheduler_status_api():
    return get_scheduler_status()


@app.post("/api/scheduler/run")
async def scheduler_run_api():
    """手动触发一次定时任务（后台线程跑，遍历所有有偏好的用户）。"""
    t = threading.Thread(target=daily_briefing_job, daemon=True)
    t.start()
    return {"ok": True, "message": "daily_briefing_job triggered in background"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
