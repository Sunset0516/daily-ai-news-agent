-- 每日 AI 新闻助手 Agent 建表脚本（幂等，可重复执行）
-- 启用时记得 PRAGMA foreign_keys = ON 才会触发级联删除

-- 用户表：一个用户对应一份订阅偏好和若干历史简报
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    feishu_webhook TEXT,                                          -- 飞书自定义机器人 webhook，可空（运行时填）
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 订阅偏好表：行级存储，便于增删；type 区分话题与关键词
CREATE TABLE IF NOT EXISTS preferences (
    pref_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    type        TEXT NOT NULL CHECK(type IN ('topic','keyword')),
    value       TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE(user_id, type, value)                                  -- 防止同一用户重复添加相同偏好
);

-- 简报表：一人一天一份；agent_log 存 Thought/Action/Observation 回看
CREATE TABLE IF NOT EXISTS briefings (
    briefing_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    briefing_date    DATE    NOT NULL,
    title            TEXT    NOT NULL,
    summary          TEXT,                                        -- 概述段落，列表展示用
    markdown_content TEXT    NOT NULL,                             -- 完整 markdown
    file_path        TEXT,                                        -- 本地 .md 文件路径
    feishu_pushed    INTEGER DEFAULT 0,                           -- 0=未推送 1=已推送（由 agent 最终回复决定）
    push_error       TEXT,                                        -- 推送失败原因（如有）
    tool_call_count  INTEGER DEFAULT 0,
    agent_log        TEXT,                                        -- JSON 数组，存步骤回看
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE(user_id, briefing_date)                               -- 一人一天一份
);

CREATE INDEX IF NOT EXISTS idx_briefings_user_date ON briefings(user_id, briefing_date);
