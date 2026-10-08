"""结构化日志配置：JSON 格式 + 级别 + 模块 + 时间，方便生产观测和错误聚合。

设计：
- 生产环境（默认）：JSON 单行输出，字段 ts/level/logger/msg/extra，便于 ELK/Loki 采集
- 开发环境（LOG_FORMAT=text）：人类可读的多行格式，带颜色
- uvicorn access log 通过 dictConfig 接入同一 handler，避免双套日志
- 模块用 logging.getLogger(__name__) 即可，无需每个文件单独配置

用法：在 main.py lifespan startup 调用 setup_logging() 一次即可。
"""
import json
import logging
import logging.config
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """单行 JSON 格式器：ts/level/logger/msg + extra 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        # 基础字段
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # 异常信息
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # extra 字段（agent_run / tool / duration_ms 等）
        for key in ("agent_run", "user_id", "tool", "duration_ms", "status",
                    "tool_call_count", "feishu_pushed", "push_error", "briefing_id"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        # 自定义 extra（record.__dict__ 里以 _ 开头的跳过，其余非标准字段也带出去）
        standard = set(logging.LogRecord(
            name="", level=0, pathname="", lineno=0,
            msg="", args=(), exc_info=None
        ).__dict__.keys()) | {"agent_run", "user_id", "tool", "duration_ms",
                              "status", "tool_call_count", "feishu_pushed",
                              "push_error", "briefing_id", "message"}
        for k, v in record.__dict__.items():
            if k not in standard and not k.startswith("_"):
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = str(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """初始化全局日志配置。在应用启动时调用一次。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        fmt: 'json' 生产用，'text' 开发可读
    """
    if fmt == "text":
        # 开发：彩色可读
        log_format = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        datefmt = "%H:%M:%S"
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(log_format, datefmt=datefmt))
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
            }
        },
        "root": {
            "level": level,
            "handlers": ["default"],
        },
    }
    # 用直接 handler 替换 dictConfig 的格式器配置（简化）
    logging.config.dictConfig(config)
    # 重新拿到 root handler，应用格式器
    root = logging.getLogger()
    for h in root.handlers:
        h.flush()
    # 清掉默认 handler，换上我们的
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn 也走同一套（避免双套日志）
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error",
                  "apscheduler", "apscheduler.executors.default"):
        lg = logging.getLogger(noisy)
        lg.handlers = []
        lg.propagate = True  # 让 root handler 处理

    logging.getLogger(__name__).info("logging initialized",
                                     extra={"agent_run": "system", "status": "ok"})
