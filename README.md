# ⚙️ Pi Orchestrator

轻量级 Agent 调度器 —— Multica 的 Python 复刻。零外部依赖，内网即用。

## 快速开始

```bash
# 终端 1 — Server (HTTP + Web UI)
cd pi-orchestrator
pip install -r requirements.txt
python server.py          # → http://localhost:8020

# 终端 2 — Daemon (必须在真实终端运行！)
python daemon.py
```

## 测试

```bash
# 单元测试（agent.py 对 multica pi.go 的复刻，49 项断言）
python tests/test_agent.py

# 端到端测试（server + daemon + mock pi 完整链路，16 项断言；
# Windows 下走真实 pi.cmd → powershell -File pi.ps1 重写路径）
python tests/test_e2e.py
```

## 架构

```
┌──────────────────────────────────────┐
│  server.py (FastAPI)                 │
│  ├── REST API (/api/*)               │
│  ├── SSE Stream (/api/stream)        │
│  └── Web UI (templates/index.html)   │
└────────────┬─────────────────────────┘
             │ SQLite (orchestrator.db, WAL)
┌────────────▼─────────────────────────┐
│  daemon.py (独立进程)                │
│  ├── 僵尸任务恢复 (启动时 reconcile) │
│  ├── RuntimeManager (注册/心跳)      │
│  ├── ThreadPool 真并发 (MAX=3)       │
│  ├── 取消轮询 → 杀进程树             │
│  └── Git Worktree 隔离               │
└────────────┬─────────────────────────┘
             │ subprocess (stdin EOF)
┌────────────▼─────────────────────────┐
│  agent.py (PiAgent)                  │
│  ├── -p --mode json --session <path> │
│  ├── --provider/--model 拆分         │
│  ├── --append-system-prompt          │
│  ├── text_delta 结构化标记清洗       │
│  ├── 超时/取消看门狗                 │
│  └── Windows pi.cmd→ps1 重写(#3306)  │
└──────────────────────────────────────┘
```

## 核心功能

- ✅ **Agent 人格化** — system prompt 走 `--append-system-prompt`，model 走 `--provider/--model`，不污染用户 prompt
- ✅ **Issue 全生命周期** — queued → claimed → running → completed/failed/blocked/**cancelled**
- ✅ **任务取消** — 运行中任务可取消（kill 进程树），queued 任务直接取消
- ✅ **僵尸任务恢复** — daemon 崩溃重启后自动把 claimed/running 任务重新入队
- ✅ **真并发** — ThreadPoolExecutor，MAX_CONCURRENT 实际生效
- ✅ **Session 续聊** — `--session <path>` 持久化，支持 resume（多轮对话）
- ✅ **Token 用量** — turn_end 事件累计 per-model input/output/cache tokens
- ✅ **评论区** — 任务展开可评论，系统自动记录 block/unblock/retry/cancel
- ✅ **Git Worktree** — 每个任务独立 git 分支
- ✅ **Runtime 管理** — Daemon 注册/心跳/离线检测，关停时主动下线
- ✅ **实时推送** — SSE 每 2 秒刷新任务状态
- ✅ **Web UI** — Dark 主题，Agent 面板，任务看板，过滤器（含已取消）

## API 总览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 任务列表 (?status=queued/running/...) |
| POST | `/api/tasks` | 创建任务 |
| GET | `/api/tasks/{id}` | 任务详情 + log |
| DELETE | `/api/tasks/{id}` | 删除 |
| PUT | `/api/tasks/{id}/retry` | 重试（failed/completed/cancelled） |
| PUT | `/api/tasks/{id}/cancel` | 取消（运行中 kill 进程） |
| PUT | `/api/tasks/{id}/block` | 阻塞（运行中同时请求取消） |
| PUT | `/api/tasks/{id}/unblock` | 解阻 |
| GET | `/api/tasks/{id}/comments` | 评论列表 |
| POST | `/api/tasks/{id}/comments` | 添加评论 |
| GET/POST | `/api/agents` | Agent CRUD |
| GET | `/api/runtimes` | 运行时状态 |
| GET | `/api/stats` | 统计面板 |
| GET | `/api/stream` | SSE 实时流 |

## 配置

编辑 `config.yaml` 调整轮询间隔、并发数等。

环境变量:

| 变量 | 说明 |
|------|------|
| `PI_EXECUTABLE` | 显式指定 pi 可执行文件路径（对应 Multica `cfg.ExecutablePath`），覆盖 PATH 检测 |
| `PI_ORCHESTRATOR_DB` | 覆盖 SQLite 数据库路径（测试隔离/多实例） |

## 与 Multica 的对应关系

| Multica (Go) | Pi Orchestrator (Python) |
|---|---|
| `pkg/agent/pi.go` piBackend | `agent.py` PiAgent |
| stdin pipe + 立即关闭 (#2188) | `process.stdin.close()` |
| pi.cmd → powershell -File (#3306) | `choose_pi_invocation()` |
| `--session` + ResumeSessionID | `resume_session_id` 参数 |
| text markup/control-token 清洗 | `drain_pi_text_buffer()` 等 |
| error/auto_retry_end → failed | 事件内终态 + exit code 兜底 |
| ctx deadline → kill | 看门狗线程 + `taskkill /T /F` |
| daemon reconcile | 启动时 `requeue_stale_tasks()` |
| piBlockedArgs 过滤 | `filter_custom_args()` |
