# 每日 AI 新闻助手 Agent

一个用 LLM ReAct 循环驱动的 AI 新闻简报 Agent。Agent 自主决定调用工具的顺序与次数（搜新闻 → 整理 → 写文件 → 推飞书），失败时 LLM 通过 Observation 感知并自主决策重试或回退本地文件。前端实时展示 Agent 的思考过程（Thought / Action / Observation / Finish）。

## 特性

- **工具调用由 LLM 决策**：基于 LangGraph `create_react_agent` + DeepSeek（OpenAI 兼容 function calling），非硬编码流程。
- **7 个工具**：`list_dir` / `read_file` / `search_content` / `write_file` / `bash` / `search_web`(Tavily) / `push_to_feishu`。
- **容错**：飞书推送失败时，LLM 读 ERROR Observation 后自主重试一次或回退本地文件，最多重试 1 次。
- **思考过程可见**：前端 SSE 流式展示 Agent 每一步 Thought/Action/Observation，推送失败时红色高亮。
- **定时调度**：APScheduler 每日 8:00 自动为所有有偏好的用户生成简报（时间可配）。
- **白蓝配色**前端：用户管理、订阅偏好、生成简报、历史简报列表、详情（简报+思考过程回看双 tab）。

## 5 分钟上手

### 前置
- Python 3.10+
- DeepSeek API Key（https://platform.deepseek.com）
- Tavily API Key（https://tavily.com，免费 1000 次/月）
- （可选）飞书自定义机器人 webhook URL

### 步骤

```bash
# 1. 进入项目目录
cd daily-ai-news-agent

# 2. 创建虚拟环境并安装依赖
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS/Linux
pip install -r requirements.txt

# 3. 配置环境变量（复制模板并填入你的 key）
copy .env.example .env           # Windows
# cp .env.example .env            # macOS/Linux
# 然后编辑 .env 填入 DEEPSEEK_API_KEY 和 TAVILY_API_KEY

# 4. 初始化数据库
python -c "from app.db import init_db; init_db()"

# 5. 启动服务
python -m app.main
# 或：venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 6. 浏览器访问
# 打开 http://127.0.0.1:8000
```

### 使用流程

1. **创建用户**：填姓名 + 飞书 webhook URL（飞书群「设置 → 群机器人 → 添加自定义机器人」可拿到 webhook）。
2. **添加订阅偏好**：选择类型（topic 话题 / keyword 关键词），填内容（如 `LLM Agent`、`DeepSeek`、`RAG`），点添加。
3. **生成简报**：点「立即生成今日简报」，右侧「思考过程」区会实时流式展示 Agent 的 Thought/Action/Observation 卡片，约 30-90 秒完成。
4. **查看简报**：点历史简报列表的「查看」，弹窗内切「简报」/「思考过程回看」两个 tab。
5. **定时任务**：服务运行后每日 8:00 自动为所有有偏好的用户生成简报（改 `.env` 的 `BRIEFING_HOUR`/`BRIEFING_MINUTE` 调整时间，重启生效）。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口 + 16 路由 + SSE 流式 generate + lifespan
│   ├── agent.py       # ReAct agent + sync/stream 双入口 + finish 解析
│   ├── tools.py       # 7 个工具 + 路径/命令安全约束 + push_to_feishu 容错
│   ├── scheduler.py   # APScheduler 每日定时
│   ├── db.py          # SQLite CRUD + UPSERT + 级联删除
│   ├── push.py        # 前端渲染辅助
│   └── config.py     # .env 读取 + Settings + ALLOWED_ROOTS
├── static/            # 前端（白蓝配色，原生 JS + fetch SSE）
│   ├── index.html
│   ├── style.css
│   └── app.js
├── briefings/         # 简报 markdown 文件（运行时生成，.gitignore 忽略内容）
├── schema.sql          # 三张表建表脚本
├── requirements.txt
├── .env.example        # 环境变量模板（不含真实密钥）
└── .gitignore          # 忽略 .env / *.db / briefings/*.md / venv / __pycache__
```

## 工具集与安全约束

| 工具 | 用途 | 安全约束 |
|---|---|---|
| `list_dir(path)` | 列目录 | path 必须在 `ALLOWED_ROOTS`（项目根 + briefings）内，越权返回 ERROR |
| `read_file(path)` | 读文件 | 同上 + 文件 >1MB 截断 |
| `search_content(keyword, dir)` | 搜内容 | dir 限制 + 最多 50 条 |
| `write_file(path, content)` | 写文件 | path 必须以 `briefings/` 开头且解析后仍在 briefings 子树（防 `../` 越狱） |
| `bash(command)` | shell 命令 | 工作目录锁项目根；denylist 阻断 `rm -rf`/`curl`/`wget`/`sudo` 等；超时 10s |
| `search_web(query)` | Tavily 搜索 | 唯一合法新闻来源 |
| `push_to_feishu(webhook_url, content)` | 推飞书 | 失败返回 ERROR 字符串（不抛异常），LLM 据此自主重试或回退 |

所有工具失败均返回 `ERROR: ...` 字符串作为 Observation，不抛异常——这是容错的核心。

## 核心要求：工具调用顺序由 LLM 决定

Agent 用 `langgraph.prebuilt.create_react_agent` 构建，本质是 ReAct 循环 + `bind_tools`。
LLM 拿到任务后自主决定：调哪个工具、调用几次、什么时候停。
典型路径（LLM 自主决策，非硬编码）：
1. `search_web` 搜多个关键词
2. `read_file`/`list_dir` 复查历史避免重复
3. `write_file` 写简报到 `briefings/YYYY-MM-DD.md`
4. `push_to_feishu` 推送；失败时读 ERROR Observation，重试一次或回退本地文件
5. 输出 `FINISH: pushed` 或 `FINISH: local only, push failed: <原因>`

## 配置项（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek API 密钥 |
| `DEEPSEEK_BASE_URL` | https://api.deepseek.com | LLM 接口 |
| `DEEPSEEK_MODEL` | deepseek-chat | 模型名 |
| `LLM_TEMPERATURE` | 0.3 | 采样温度 |
| `LLM_MAX_TOKENS` | 4096 | 最大输出 token |
| `TAVILY_API_KEY` | 必填 | Tavily 搜索密钥 |
| `BRIEFING_HOUR` | 8 | 每日生成小时 |
| `BRIEFING_MINUTE` | 0 | 每日生成分钟 |
| `APP_HOST` | 127.0.0.1 | 监听地址 |
| `APP_PORT` | 8000 | 监听端口 |

## 技术栈

- Python 3.10 / LangChain 1.4 / LangGraph 1.2 / langgraph-prebuilt 1.1
- DeepSeek（OpenAI 兼容 function calling）/ Tavily Search
- FastAPI / Uvicorn / APScheduler / SQLite
- 前端：原生 HTML/CSS/JS + marked.js（markdown 渲染）+ fetch ReadableStream（解析 SSE）

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `/api/health` 返回 `missing` | `.env` 未填 key 或未 `load_dotenv` |
| 生成简报一直转圈 | 查终端日志，多为 DeepSeek/Tavily 网络超时；确认 key 有效 |
| 飞书推送一直失败 | webhook URL 无效或被飞书限流；Agent 会自动回退本地文件，简报仍在 `briefings/` |
| `--reload` 下定时任务重复 | dev 环境可接受；生产用 `python -m app.main` 不开 reload |
| Agent 不收敛 | `AgentExecutor` max_iterations 兜底，看思考过程日志定位 |
