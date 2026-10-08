"""APScheduler 定时任务：每日生成简报。

用 BackgroundScheduler（同步线程池，不和 FastAPI event loop 打架）。
触发器 CronTrigger(hour, minute) 从 env 读（默认 8:00）。
生命周期：FastAPI lifespan startup 启动 / shutdown 关闭。
"""
import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.agent import run_briefing_sync
from app.config import settings
from app.db import list_preferences, list_users

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


def daily_briefing_job():
    """每日定时任务：遍历所有有偏好的用户，依次生成简报。

    思考过程写入 DB agent_log 字段，前端可事后回看（不流式）。
    """
    users = list_users()
    logger.info(f"daily_briefing_job: {len(users)} users")
    for user in users:
        prefs = list_preferences(user["user_id"])
        if not prefs:
            logger.info(f"skip user {user['name']}: no preferences")
            continue
        try:
            briefing, agent_log = run_briefing_sync(user, prefs)
            if briefing:
                logger.info(
                    f"briefing for {user['name']}: id={briefing['briefing_id']} "
                    f"pushed={briefing['feishu_pushed']} calls={briefing['tool_call_count']}")
            else:
                logger.warning(f"briefing for {user['name']}: not saved")
        except Exception as e:
            logger.exception(f"briefing for {user['name']} failed: {e}")


def start_scheduler() -> BackgroundScheduler:
    """启动调度器（FastAPI lifespan startup 调用）。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()
    trigger = CronTrigger(hour=settings.briefing_hour, minute=settings.briefing_minute)
    _scheduler.add_job(daily_briefing_job, trigger, id="daily_briefing", replace_existing=True)
    _scheduler.start()
    logger.info(
        f"scheduler started, daily at {settings.briefing_hour:02d}:{settings.briefing_minute:02d}")
    return _scheduler


def shutdown_scheduler():
    """停止调度器（FastAPI lifespan shutdown 调用）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler shutdown")


def get_scheduler_status() -> dict:
    """返回调度器状态（前端展示用）。"""
    if _scheduler is None:
        return {"running": False, "next_run_time": None, "jobs": []}
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    next_run = jobs[0]["next_run_time"] if jobs else None
    return {"running": _scheduler.running, "next_run_time": next_run, "jobs": jobs}
