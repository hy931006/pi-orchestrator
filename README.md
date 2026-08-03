# ⚙️ Pi Orchestrator

轻量级 Agent 任务调度器 + 可视化工作流编排平台 —— 基于 [Pi Coding Agent](https://github.com/earendil-dev/pi-coding-agent) 的任务级执行引擎，扩展为「工作流 → 阶段 → 门控流转」的纵向编排系统。

零外部服务依赖（SQLite + FastAPI + 原生 JS），内网即用。

```
┌─────────────────────────────────────────────────────┐
│  Web UI (localhost:8020)                             │
│  ┌──────────┬──────────┬───────────┬──────────┐     │
│  │ 📋 任务   │ 🔀 工作流 │ 🎨 编排器  │ 🤖 Agent │     │
│  └──────────┴──────────┴───────────┴──────────┘     │
├─────────────────────────────────────────────────────┤
│  server.py (FastAPI: REST + SSE + 审批 API)          │
│  database.py (SQLite: tasks/workflow_runs/agents)    │
│  workflow.py (DAG 依赖解锁引擎 + 模板加载)            │
│  gate.py (YAML 驱动门控引擎，5 类检查)               │
│  qa.py (ruff/mypy/自定义规则扫描 + QA 报告)          │
│  daemon.py (调度器: 轮询/并发/取消/gate 后置)        │
│  agent.py (PiAgent: pi JSON 事件流解析)              │
└─────────────────────────────────────────────────────┘
```

## 快速开始

```bash
# 终端 1 — Web UI + API
cd pi-orchestrator
pip install -r requirements.txt
python run.py              # → http://localhost:8020

# 终端 2（必须真实终端窗口，见「已知限制」）
python daemon.py            # 任务调度器
```

## 功能总览

### 📋 任务（单任务调度，v0 保留）

- 任务队列：`queued → claimed → running → completed/failed/blocked/cancelled`
- 真并发（ThreadPoolExecutor，MAX_CONCURRENT=3）、取消（kill 进程树）、重试、僵尸任务恢复
- Git Worktree 隔离、Session 续聊、Token 用量统计
- 单任务可绑定自定义 Agent（人格 + 模型注入 pi 子进程）

### 🔀 工作流（纵向编排）

- **五阶段模板**：可行性分析 → 详细设计 → 实施计划 → 实施 → QA（`workflows/default.yaml`）
- **DAG 执行引擎**：`depends_on` 依赖解锁，支持并行分支（A→B∥C→D），环检测，线程安全幂等
- **门控流转**：每阶段完成后自动跑 gate（机器检查）→ **gate 不过即阻断主线**（不再无条件放行）；人工审批（批准/驳回/强制/豁免，理由必填审计）
- **Repair 分支（条件路由）**：阶段可配 `on_gate_fail: <repair节点>` + `max_repairs: N`（默认 2）。gate 失败自动路由到 `type: repair` 的 LLM 节点——注入 gate-result.md 上下文，复用父阶段 gate_rules 自旋复检；通过则父阶段标记 `repaired` 并解锁下游，N 次未过转人工。repair 节点不作入口、不参与正常解锁、不计入完成条件
- **产物追溯**：每阶段独立 git commit，`docs/<run_id>/` 完整链路
- **阶段详情**：状态徽章 + gate 徽章 + 绑定 Agent 徽章 + 结果预览

### 🎨 编排器（Dify 式拖拽画布）

- 从左侧拖入节点（开始/LLM 阶段/结束），拖出连线定义依赖
- 节点配置：绑定 Agent（持久实体）或内嵌 Prompt、产出文件 glob、模型下拉（实时读 pi 配置）
- 自动布局：无坐标模板按依赖层级展开（线性横向、并行分支纵向）
- 保存为 `workflows/<name>.yaml`，创建 workflow 时选用
- 已保存模板一键回显画布

### 🤖 Agent（持久实体，与任务解耦）

- 独立管理页：创建 / 编辑（名称、模型、System Prompt）/ 停用 / 删除
- 模型下拉实时读取 `~/.pi/agent/models.json`（兼容 `~/.omp/agent/`）
- 模板引用计数提示（被 N 个工作流模板引用）
- **预设 5 角色**：可行性分析师 / 系统架构师 / 实施计划师 / 编码工程师 / QA工程师（绑定 default 模板）
- 模型优先级：节点覆盖 > Agent 绑定 > pi 默认

### ✅ 门控引擎（gate.py）

YAML 规则驱动，五类检查：structure / content / cross_ref / yaml_parse / human。
异常即 FAIL（R10 fail-safe），规则文件与检查器自动同步。

### 🔍 QA 集成（qa.py）

- ruff (lint) + mypy (type) + 自定义规则（qa-rules.yaml）
- 报告四章节：测试套件 / 静态扫描 / 自定义规则 / 阻断总结（PASS/BLOCKED）

## API 总览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 任务列表（?status=） |
| POST | `/api/tasks` | 创建单任务 |
| GET/POST | `/api/agents` | Agent 列表（含引用计数）/ 创建 |
| PUT/DELETE | `/api/agents/{id}` | 编辑 / 物理删除 |
| GET | `/api/models` | pi 可用模型列表（读 models.json） |
| GET | `/api/workflows/templates` | 模板列表 |
| POST | `/api/workflow-templates` | 保存画布模板（nodes+edges→DAG） |
| GET | `/api/workflow-templates/{name}` | 加载模板（含 canvas 坐标） |
| POST | `/api/workflows` | 创建工作流实例（自动建入口阶段任务） |
| GET | `/api/workflows/{id}/stages` | 阶段列表（含 agent_name/model 反查） |
| POST | `/api/workflows/{id}/approve\|reject\|force\|waive` | 门控审批（审计评论） |
| GET | `/api/stream` | SSE 实时推送 |
| GET | `/health` | 健康检查 |

## 测试

```bash
python tests/test_agent.py            # 49 项：PiAgent 适配器
python tests/test_workflow.py         # 19 项：DAG 引擎/agent_ref/环检测
python tests/test_gate.py             # 8 项：门控引擎
python tests/test_qa.py               # 6 项：QA 扫描
python tests/test_workflow_e2e.py     # 8 项：server+daemon+mock pi 全链路
# 真实 pi 验收（真实终端）:
python acceptance_test.py             # 真实 pi CLI 全链路
```

## 配置

| 环境变量 | 说明 |
|---------|------|
| `PI_EXECUTABLE` | 显式指定 pi 路径（覆盖 PATH 检测） |
| `PI_ORCHESTRATOR_DB` | SQLite 路径（测试隔离/多实例） |
| `ACCEPT_TIMEOUT` | acceptance 单阶段超时（秒） |

`config.yaml`：轮询间隔、并发数、任务超时等。

## 执行层抽象与 Backend 替换

编排层（daemon / workflow）只依赖 `backends.create_backend()` 与 `AgentResult` 契约，
不直接耦合具体 coding agent。默认执行层是 Pi，可无缝替换为任意 CLI coding agent。

### 架构

```
编排层 (daemon.py / workflow.py)
   │  只依赖: backends.create_backend() + AgentResult
   ▼
backends/  (执行层抽象)
   ├─ base.py : AgentBackend (抽象基类) + AgentResult (统一结果契约)
   │           + register_backend() / create_backend() (注册表/工厂)
   ├─ echo.py : EchoBackend（演示/测试，不做真实 LLM 调用）
   └─ agent.py: PiAgent（@register_backend("pi")，pi CLI 适配器）
```

**`AgentResult` 统一字段**（跨 backend 一致）：

```
text / thinking / tool_calls / tool_results / errors
status (completed|failed|timeout|aborted) / exit_code / error
session_id / duration_ms / usage
```

**`execute()` 统一签名**：

```python
execute(prompt, cwd, model, system_prompt, custom_args,
        resume_session_id, timeout, cancel_event, on_event) -> AgentResult
```

### 替换为其他 coding agent（3 步）

```python
# 1. 实现 AgentBackend 子类（如 backends/claude_code.py）
from backends.base import AgentBackend, AgentResult, register_backend

@register_backend("claude-code")
class ClaudeCodeBackend(AgentBackend):
    def execute(self, prompt, cwd=None, model="", system_prompt="",
                custom_args=None, resume_session_id="", timeout=None,
                cancel_event=None, on_event=None) -> AgentResult:
        r = AgentResult()
        # ... 调用你的 CLI（如 claude -p --output-format json），
        #     把输出填进 r.text / r.thinking / r.status 等
        r.status = "completed"
        return r
```

```bash
# 2. 启动时切换（环境变量，无需改代码）
PI_ORCHESTRATOR_BACKEND=claude-code python daemon.py
```

```python
# 3.（可选）单元测试里直接工厂获取
from backends import create_backend
agent = create_backend("claude-code")
result = agent.execute("你好")
```

### 内置 backend

| 名称 | 类 | 说明 |
|------|----|------|
| `pi` | `agent.PiAgent` | 默认。pi CLI 全功能适配（JSON 事件流/续聊/取消/超时） |
| `echo` | `backends.echo.EchoBackend` | 回显 prompt，验证可插拔性 & 测试用（无需真实模型） |

### 行为契约（新 backend 必须遵守）

- `status` 必须是 `completed` / `failed` / `timeout` / `aborted` 之一（daemon 据此写任务终态）
- `text` 为清洗后的纯文本；`errors` 非空且非 `completed` 时任务记为失败
- `cancel_event` 被置位时必须中止执行并返回 `status="aborted"`
- `session_id` 返回会话标识以便 `resume_session_id` 续聊（不支持可返回空）
- 未实现 `execute` 的子类会在调用时抛 `NotImplementedError`

### 已知 backend 的配置

| 环境变量 | 说明 |
|---------|------|
| `PI_ORCHESTRATOR_BACKEND` | 执行 backend 名（默认 `pi`） |
| `PI_EXECUTABLE` | pi 可执行文件路径（覆盖 PATH 检测，仅 pi backend 用） |

## 已知限制

- **daemon 必须在真实终端运行**（Hermes 后台会阻断子进程 stdout 捕获，pi 输出为空）。PowerShell/git-bash 窗口直接 `python daemon.py`。
- E2E 测试需全链路环境；真实 pi E2E 走 `acceptance_test.py`。
- API 无鉴权（内网可信环境），所有变更动作有审计评论。

## 与 Multica 的对应关系

| Multica (Go) | Pi Orchestrator (Python) |
|---|---|
| `pkg/agent/pi.go` piBackend | `agent.py` PiAgent |
| stdin pipe + 立即关闭 (#2188) | `process.stdin.close()` |
| pi.cmd → powershell -File (#3306) | `choose_pi_invocation()` |
| daemon reconcile | 启动时 `requeue_stale_tasks()` |
| piBlockedArgs 过滤 | `filter_custom_args()` |
