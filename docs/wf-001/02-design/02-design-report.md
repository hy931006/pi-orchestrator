# 详细设计报告 — Pi 编排调度层

> **工作流**: wf-001 | **阶段**: 02-design | **日期**: 2026-08-01
> **上游**: 01-feasibility-report.md（Stage 1 gate 已闭合，commit `47e3a49`）
> **给 Stage 4 实施者**: 本文是零上下文设计规格。每节可作验收标准，每项决策可追溯到 Q1-Q10 或 Stage 1 风险条目。

---

## 1. 设计目标与约束

### 1.1 继承自 Stage 1 的决策

| 来源 | 决策 | 本文节 |
|------|------|--------|
| Q1 | 工作流模板可配置（YAML 定义阶段序列） | §4 |
| Q2 | 每阶段派独立 pi 子任务 | §5 |
| Q3 | 门控 = 机器自动 + 人工审批（混合） | §6 |
| Q4 | 门控失败 → 人工可选：回退/强制流转/豁免并记录 | §6.4 |
| Q5 | 自动校验：文档结构、代码 lint、测试覆盖率、需求对位 | §6.3 |
| Q6 | 产物存 filesystem + git commit，不建 DB 索引 | §5.3 |
| Q7 | QA：测试套件 + 静态扫描 + 自定义规则，报告含位置/严重度/阻断 | §7 |
| Q8 | 扩展 pi-orchestrator（非新建项目） | §2 |
| Q9 | 保留单任务入口，与 workflow 入口共存 | §9 |
| Q10 | 本项目自身走五阶段流程 | §12 |

### 1.2 本阶段解决 Stage 1 留下的 7 项延期决策

| # | Stage 1 延期项 | 本报告决策 |
|---|---------------|-----------|
| D1 | workflow YAML schema | §4 — 完整 schema + 可机验示例 |
| D2 | 人工审批 UI 形态 | §8 — Web UI 审批面板设计 |
| D3 | DB 迁移方案 | §3 — DDL + 兼容策略 |
| D4 | 阶段上下文传递 prompt 模板 | §5.2 — `{stage_outputs}` 注入协议 |
| D5 | checkstyle/sonar 降级选型 | §7.1 — ruff+pylint (Python) + checkstyle JAR (Java)，SonarQube 评估为超需求 |
| D6 | 人工审批 SLA/回退策略 | §6.4 — 超时 48h 自动提醒，回退语义定义 |
| D7 | Q5「合理性」判定标准 | §6.3 — 人审预检维度清单 |

### 1.3 Stage 1 已知缺陷的对策

| 缺陷 | 对策 | 本文节 |
|------|------|--------|
| #1 gate_check.py 硬编码 | §6 通用 YAML 驱动门控引擎，PyYAML `safe_load` 真解析 | §6 |
| #2 | E2E "16 项" 未核实 | **已核实**：`grep -c '^\s*check(' tests/test_e2e.py` = 16 次断言，与 README 声明一致。E2E 需全链路环境（daemon + pi）方可运行，但断言计数已确认 | ✅ 已解决 |
| #3 R9 bootstrap 闭环 | Stage 5 用 wf-002 验证编排器自管理 | §12 |

---

## 2. 架构总览

```
                         ┌──────────────────────────────────────────────┐
                         │            Web UI (index.html)               │
                         │   ┌──────────┐  ┌──────────┐  ┌──────────┐  │
                         │   │ 单任务入口│  │Workflow入口│  │ 审批面板  │  │
                         │   └────┬─────┘  └────┬─────┘  └────┬─────┘  │
                         └────────┼─────────────┼─────────────┼────────┘
                                  │             │             │
                    ┌─────────────┼─────────────┼─────────────┼──────────┐
                    │   server.py │  新增路由   │             │          │
                    │              │ POST /api/workflows       │          │
                    │              │ POST /api/workflows/{id}/approve|... │
                    │              ▼             ▼             ▼          │
                    │  ┌──────────────────────────────────────────────┐  │
                    │  │              database.py (SQLite)            │  │
                    │  │  tasks + workflow_runs + agents + comments   │  │
                    │  └──────────────────────────────────────────────┘  │
                    └──────────────────────┬───────────────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │              daemon.py     │  新: workflow 阶段轮询     │
              │                            ▼                            │
              │  ┌──────────────────────────────────────────────────┐  │
              │  │  workflow.py（新增）                              │  │
              │  │  ├── load_template(name) → stages[]              │  │
              │  │  ├── advance_stage(run_id) → queue_next_task     │  │
              │  │  └── inject_context(stage_key, prior_outputs)    │  │
              │  └──────────────────────┬───────────────────────────┘  │
              │                         │                              │
              │  ┌──────────────────────┼──────────────────────────┐  │
              │  │  gate.py（新增）      │  qa.py（新增）            │  │
              │  │  ├── YAML 规则加载    │  ├── ruff/pylint 扫描    │  │
              │  │  ├── 自动检查执行     │  ├── checkstyle 扫描     │  │
              │  │  └── gate-result.md  │  └── QA 报告生成         │  │
              │  └──────────────────────┴──────────────────────────┘  │
              │                         │                              │
              │  ┌──────────────────────┴──────────────────────────┐  │
              │  │  agent.py（已有，不变）                          │  │
              │  │  PiAgent.execute(prompt, cwd, model, ...)       │  │
              │  └─────────────────────────────────────────────────┘  │
              └───────────────────────────────────────────────────────┘
```

**新增模块清单**：

| 模块 | 预计行数 | 职责 |
|------|---------|------|
| `workflow.py` | ~200 | 模板加载、阶段流转、上下文注入、人工审批动作 |
| `gate.py` | ~300 | YAML 规则引擎、自动检查执行、gate-result.md 生成 |
| `qa.py` | ~200 | ruff/pylint/checkstyle 子进程调用、QA 报告格式化 |

**复用模块（不变）**：`agent.py`、`daemon.py`（新增 workflow 轮询逻辑 ~30 行）、`database.py`（新增表/字段）、`server.py`（新增路由 ~60 行）、`config.yaml`（新增 workflow 段）。

---

## 3. 数据库设计

### 3.1 新表：`workflow_runs`

```sql
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,                          -- uuid hex 12
    template_name TEXT NOT NULL,                   -- 对应 workflows/*.yaml 中的 name
    title TEXT NOT NULL,
    repo_path TEXT,
    branch TEXT,
    status TEXT NOT NULL DEFAULT 'running',       -- running/completed/failed/cancelled
    current_stage TEXT,                           -- 当前阶段 key，如 "feasibility"
    current_stage_index INTEGER DEFAULT 0,        -- 当前阶段序号 0-based
    artifact_dir TEXT,                            -- docs/<run-id>/
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
```

### 3.2 存量表 `tasks` 新增字段

```sql
ALTER TABLE tasks ADD COLUMN workflow_run_id TEXT;       -- NULL = 单任务模式
ALTER TABLE tasks ADD COLUMN stage_key TEXT;              -- feasibility|design|plan|implementation|qa
ALTER TABLE tasks ADD COLUMN stage_index INTEGER;         -- 0-4
ALTER TABLE tasks ADD COLUMN gate_status TEXT;            -- pending|auto_passed|auto_failed|approved|rejected|waived|forced
ALTER TABLE tasks ADD COLUMN gate_result_json TEXT;       -- JSON: gate 检查明细
```

迁移策略：全部 `ALTER TABLE ADD COLUMN`，默认 NULL。现有 5 张表数据不受影响。单任务入口不填这些字段 → NULL 即标记"非 workflow 模式"。

`gate_result_json` 字段存 JSON 数组，每条检查记录含：`id`（规则编号）、`severity`（blocker/warning）、`type`（structure/content/cross_ref/yaml_parse/human）、`rule`（规则描述）、`passed`（true/false/null 待人工）、`detail`（证据字符串）、`human_review_decision`（approved/rejected/forced/waived 仅 human 类型）、`human_reviewer`、`human_review_at`。机器检查直接写入，人工检查在审批 API 调用时覆写。

`gate_status` 转换矩阵如下：

| 当前状态 | 触发动作 | 新状态 |
|---------|---------|--------|
| pending | 自动检查全部 PASS | auto_passed |
| pending | 自动检查存在 FAIL | auto_failed |
| auto_passed | 用户 approve | approved（→ 阶段流转） |
| auto_passed | 用户 reject | rejected（→ 当前阶段回到 queued） |
| auto_failed | 用户 force | forced（→ 强制流转） |
| auto_failed | 用户 waive | waived（→ 豁免流转） |
| auto_failed | 用户 approve | 拒绝——必须先 force 或先修复后 re-queue |

**此为纯增补迁移，零数据风险**，复用 `_migrate_tasks()` 模式。

### 3.3 与现有 5 表的兼容性

- `tasks` 行当 `workflow_run_id IS NULL` 时 = 独立单任务（Q9），行为完全不变
- daemon 轮询时加 `WHERE workflow_run_id IS NOT NULL AND stage_key = ...` 可区分两类任务
- 现有 `daemon.py` 的 `get_next_queued_task()` 继续取所有 queued = 单任务 + workflow 阶段任务混合消费（天然并发）

---

## 4. 工作流模板 YAML Schema

### 4.1 存放位置与发现规则

```
pi-orchestrator/workflows/       ← 内置模板（随 git 版本化）
  default.yaml                    ← 默认五阶段模板
  quick-fix.yaml                  ← 简化三阶段模板（跳过可行性分析+设计）
```

daemon 启动时扫描 `workflows/*.yaml`，加载为模板字典 `{name: template}`。

### 4.2 Schema 定义

```yaml
# workflow 模板 — 完整 schema v1
name: default              # 唯一标识，对应 workflow_runs.template_name
description: 标准五阶段开发流程
version: 1

stages:
  - key: feasibility       # 阶段唯一标识，对应 tasks.stage_key
    label: 可行性分析
    index: 0               # 0-based 序号
    agent:
      model: ""            # 空 = 使用默认模型
      system_prompt: |
        你是可行性分析工程师。分析需求的技术可行性，产出包含现状/需求/方案对比/风险/结论的报告。
    required_artifacts:     # 此阶段必须产出的文件（门控检查用）
      - "*-report.md"      # 如 01-feasibility-report.md
      - "gate-checks.yaml"
      - "gate_check.py"
      - "gate-result.md"
    gate_rules: "01-feasibility/gate-checks.yaml"  # 相对 artifact_dir 的路径
    context_inject:         # 注入到 pi prompt 的上游产物
      prior_stages: []      # 第一阶段无上游

  - key: design
    label: 详细设计
    index: 1
    agent:
      model: ""
      system_prompt: |
        你是系统架构师。基于上游可行性分析，产出详细设计报告。
    required_artifacts:
      - "*-design-report.md"
      - "*-gate-checks.yaml"
      - "*-gate_check.py"
      - "*-gate-result.md"
    gate_rules: "02-design/gate-checks.yaml"
    context_inject:
      prior_stages: ["feasibility"]   # 注入可行性分析产物

  - key: plan
    label: 实施计划
    index: 2
    agent:
      model: ""
      system_prompt: |
        你是实施计划工程师。基于设计报告，写出可施行的分步实施计划。
    required_artifacts:
      - "*-plan.md"
      - "*-gate-checks.yaml"
      - "*-gate_check.py"
      - "*-gate-result.md"
    gate_rules: "03-plan/gate-checks.yaml"
    context_inject:
      prior_stages: ["feasibility", "design"]

  - key: implementation
    label: 实施
    index: 3
    agent:
      model: ""
      system_prompt: |
        你是编码实现工程师。严格按实施计划逐任务编码，TDD 方式提交。
    required_artifacts:
      - "*.py"              # 源码文件
    gate_rules: "04-implementation/gate-checks.yaml"
    context_inject:
      prior_stages: ["plan", "design"]

  - key: qa
    label: QA
    index: 4
    agent:
      model: ""
      system_prompt: |
        你是 QA 工程师。执行测试套件，运行静态分析，产出 QA 报告。
    required_artifacts:
      - "*-qa-report.md"
      - "*-gate-result.md"
    gate_rules: "05-qa/gate-checks.yaml"
    context_inject:
      prior_stages: ["implementation"]
```

### 4.3 最小可机验示例

下面的代码块在 gate 检查时会被 `yaml.safe_load` 验证：

```yaml
name: minimal
description: 最小二阶段模板（仅测试用）
stages:
  - key: fix
    label: 修复
    index: 0
    agent:
      model: ""
      system_prompt: 修复问题并提交
    required_artifacts: ["*.py"]
    gate_rules: ""
    context_inject:
      prior_stages: []
  - key: verify
    label: 验证
    index: 1
    agent:
      model: ""
      system_prompt: 验证修复结果
    required_artifacts: ["*result*.md"]
    gate_rules: ""
    context_inject:
      prior_stages: ["fix"]
```

### 4.4 创建时的 schema 校验

`POST /api/workflows` 创建时 `workflow.py` 执行：
1. `yaml.safe_load(template_content)` → 解析失败则 400
2. 校验 `stages` 非空、`index` 连续 0..N-1、`required_artifacts` 非空
3. agent.model 为空时自动填入 `config.yaml` 的默认模型

---

## 5. 阶段执行模型

### 5.1 执行流程图

```
workflow_run created (status=running)
  │
  ├─► advance_stage(run) → stage N queued
  │     │
  │     ├─► daemon 轮询到 stage N 的 task
  │     │     │
  │     │     ├─► inject_context(stage, prior_artifacts) → 构造 prompt
  │     │     ├─► PiAgent.execute(prompt) → pi 子进程
  │     │     ├─► 保存产物到 docs/<run_id>/<stage>/
  │     │     ├─► git add + commit（每个阶段一个 commit）
  │     │     └─► task status = completed
  │     │
  │     ├─► gate.check(stage) → 自动规则
  │     │     ├─ PASS → gate_status=auto_passed → 等待人工
  │     │     └─ FAIL → gate_status=auto_failed → 通知用户
  │     │
  │     ├─► 人工审批（用户操作 approve/reject/force/waive）
  │     │     ├─ approved → advance to next stage
  │     │     ├─ rejected → 当前阶段回到 queued（重做）
  │     │     ├─ forced  → 强制流转（管理员覆盖）
  │     │     └─ waived  → 豁免流转 + 记录原因
  │     │
  │     └─► 重复 N 次
  │
  └─► 全部阶段完成 → workflow_run.status=completed
```

### 5.2 上下文注入协议

`workflow.py` 的 `inject_context()` 构造发给 pi 的 prompt 前缀：

```python
def inject_context(stage: dict, artifact_dir: str, prior_outputs: dict) -> str:
    """构造包含上游产物的 prompt 前缀"""
    lines = [
        f"# 阶段: {stage['label']} ({stage['key']})",
        f"# 产物目录: {artifact_dir}",
        "",
    ]
    if stage["context_inject"]["prior_stages"]:
        lines.append("## 上游阶段产出（请阅读后基于其决策工作）")
        for sk in stage["context_inject"]["prior_stages"]:
            prior_dir = f"{artifact_dir}/{sk}"
            lines.append(f"- {sk}: {prior_dir}/")
            lines.append(f"  请读取 {prior_dir}/ 下的报告文件，理解其结论和约束")
        lines.append("")
    
    lines.append(f"## 本阶段任务")
    lines.append(stage["agent"]["system_prompt"])
    return "\n".join(lines)
```

**上下文传递不靠 DB、不靠全局变量**——仅靠文件系统路径引用。pi 子任务通过工具调用 `read_file` 自主读取上游产物。此设计对齐 Q6 的「DB 不索引产物」。

### 5.3 产物存留与 git 追溯

daemon 在阶段完成后自动执行：

```python
subprocess.run(["git", "-C", repo_path, "add", artifact_dir], check=True)
subprocess.run(["git", "-C", repo_path, "commit", "-m", 
    f"docs({run_id}/{stage_key}): stage {stage_index} complete — {label}"])
```

每个阶段一个独立 commit。全流程完成后，`docs/<run_id>/` 目录含完整追溯链。git log 即可查询所有阶段产出，无需 DB 索引（Q6 的实现）。

---

## 6. 门控引擎设计

**这是对 Stage 1 缺陷 #1 的正式修复**：通用 YAML 驱动 ≠ 硬编码检查。

### 6.1 规则文件格式（gate-checks.yaml）

延续并规范化 Stage 1 的 schema：

```yaml
schema_version: 1
checks:
  - id: D1
    type: structure          # structure|content|cross_ref|yaml_parse
    severity: blocker        # blocker|warning
    target: "*-design-report.md"
    rule: 文件存在且非空
    evidence_required: stat size > 0

  - id: D4
    type: yaml_parse         # 新类型：解析目标 YAML 代码块
    severity: blocker
    target: "*-design-report.md"
    rule: §4 工作流模板代码块可被 yaml.safe_load 解析
    evidence_required: '提取 ```yaml ... ``` 代码块 → yaml.safe_load 不抛异常'
```

### 6.2 gate.py 核心设计

```python
import yaml
from pathlib import Path

class GateEngine:
    """通用门控引擎 — YAML 驱动，零硬编码"""
    
    def __init__(self, rules_path: Path, artifact_dir: Path):
        self.rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        self.artifact_dir = artifact_dir
    
    def run(self) -> list[dict]:
        """执行全部检查，返回结果列表"""
        results = []
        for check in self.rules["checks"]:
            if check.get("type") == "human":
                results.append(self._placeholder(check))
                continue
            
            dispatcher = {
                "structure": self._check_structure,
                "content": self._check_content,
                "cross_ref": self._check_cross_ref,
                "yaml_parse": self._check_yaml_parse,
            }
            handler = dispatcher.get(check["type"])
            if handler:
                results.append(handler(check))
        return results
```

**yaml_parse 检查类型**（本引擎的核心创新）：
1. 正则提取报告中的 ` ```yaml ... ``` ` 代码块
2. `yaml.safe_load(block)` 尝试解析
3. 解析成功 → PASS；失败 → FAIL + 附错误信息

此设计使得 Stage 4 实施时**直接复用 gate.py 而无需为每个阶段重写检查器**——不同阶段只需提供不同的 `gate-checks.yaml`。

### 6.3 自动检查维度（对齐 Q5）

| 维度 | 实现方式 | 适用阶段 |
|------|---------|---------|
| 文档结构完整性 | structure 型 check：章节标题正则匹配 | 全阶段 |
| 文档交叉引用一致性 | cross_ref 型 check：commit hash/Q# 标记存在性 | 分析/设计/计划 |
| 代码质量（lint） | qa.py 调 ruff/mypy/checkstyle，产出 lint 报告 | 实施/QA |
| 代码实现完整性 | qa.py 统计需求覆盖率（报告中 `### Task N` 均有对应 git commit） | QA |
| **合理性（人审兜底）** | AI 生成预检清单，人审前查阅：① 每项决策是否可追溯到 Q#/R# ② 有无自相矛盾的声明 ③ 缺失章节。人工据此判断 | 全阶段 |

### 6.4 人工审批 API 语义（对齐 Q4 可选项）

| 动作 | API | 效果 |
|------|-----|------|
| 批准 | `POST /api/workflows/{id}/approve` | gate_status → approved，流转下一阶段 |
| 驳回 | `POST /api/workflows/{id}/reject?reason=...` | gate_status → rejected，当前阶段回到 queued（原 task 的 session_id 保留，resume 重做） |
| 强制流转 | `POST /api/workflows/{id}/force?reason=...` | gate_status → forced，忽略 gate 失败，强制流转 |
| 豁免 | `POST /api/workflows/{id}/waive?reason=...` | gate_status → waived，跳过当前阶段，直接流转 |

驳回语义：当前阶段 task 被 reset 为 queued 状态，复用 `session_id` → pi 以 `--session` resume 执行，保留已写文件但可修改。

**SLA**（D6 决策）：超时 48 小时不审批 → daemon 在 task 上加 comment "@user 待审批已 48h"，不做自动流转。审批超时不改变状态。

### 6.5 人审预检报告

审批面板加载时，gate.py 自动生成预检摘要：

```markdown
## 人审预检 — wf-001 Stage 2 (design)

**决策追溯**: 7/7 延期决策有显式答案 ✅
**矛盾检测**: 未检测到相互矛盾的声明 ✅
**章节完整性**: 10/10 必要章节齐全 ✅
**依赖声明**: 所有依赖（PyYAML/ruff）均在 requirements 中明示 ✅
**迁移影响**: 纯增补 DDL，不修改现有 5 表结构 ✅
```

人工基于此摘要 + 完整报告可快速判断（D7 的实现）。

---

## 7. QA 集成设计

### 7.1 工具链降级最终选型（锁定 D5）

环境探测结论：sonar-scanner 与 checkstyle 均未安装。SonarQube 完整部署需 Docker + JDBC + 内网可达，评估为**本项目超需求**。

**选型**：

| 语言 | 工具 | 安装方式 | 门控阻断阈值 |
|------|------|---------|-------------|
| Python | **ruff** (lint + format) + **mypy** (type check) | `pip install ruff mypy`（air-gapped 预置 wheel） | ruff error > 0 即阻断；mypy error > 0 为 warning |
| Java | **checkstyle** 独立 JAR | 下载 `checkstyle-10.x-all.jar` + `google_checks.xml`，内网预置 | violation > 0 为 warning |
| 通用 | **自定义规则**（YAML 定义） | qa.py 内置 | 规则以 blocker 标记的阻断 |

### 7.2 QA 报告格式

`qa.py` 产出的报告含四个必需章节：测试套件结果表（文件/通过/失败/跳过/耗时）、静态扫描明细表（文件/行号/规则/严重度/内容）、自定义规则扫描明细（规则ID/文件/位置/严重度/是否阻断/描述）、阻断总结（阻断项数量+警告项数量+最终 PASS/BLOCKED 判定）。严重度枚举为 blocker（阻断流转）、critical（严重但不禁行）、major（重要）、minor（建议）、info（信息）。位置格式统一为 `文件路径:行号:列号`。

### 7.3 自定义规则格式

```yaml
# qa-rules.yaml
rules:
  - id: QA001
    pattern: 'print\(.*\)'
    file_glob: "*.py"
    severity: warning
    blocking: false
    description: 禁止遗留调试 print() 语句

  - id: QA002
    pattern: '@pytest\\.mark\\.skip'
    file_glob: "test_*.py"
    severity: blocker
    blocking: true
    description: 禁止提交跳过的测试
```

### 7.4 QA 执行流程

1. qa.py 接收 artifact_dir 路径，扫描所有 `*.py` 文件
2. 对每个 Python 文件依次运行 ruff check、mypy --strict、自定义 grep 规则
3. Java 文件（如有）运行 `java -jar checkstyle.jar -c google_checks.xml`
4. 收集全部发现 → 按严重度排序 → 写入 `*-qa-report.md`
5. 返回 exit code：有 blocker → 1（门控阻断）；仅有 warning → 0（通过但带警告）

qa.py 不负责跑单元测试——测试执行由 daemon 在实施阶段完成，qa.py 只解析 pytest 的 JUnit XML 输出并纳入报告。

---

## 8. 人工审批 UI 设计

### 8.1 入口

现有 Web UI（Dark 主题仪表盘）新增「Workflows」标签页。点入后：

- 上半区：workflow 列表（名称/状态/当前阶段/创建时间）
- 点击某 workflow → 下半区展开：阶段时间线 + 当前阶段的操作面板

### 8.2 审批面板布局（响应式单列）

```
┌─────────────────────────────────────────┐
│  wf-001 Stage 2: 详细设计               │
│  状态: ⏳ 待审批                         │
├─────────────────────────────────────────┤
│  📋 人审预检摘要（gate.py 生成）         │
│  ┌───────────────────────────────────┐  │
│  │ 决策追溯: 7/7 ✅                   │  │
│  │ 矛盾检测: 无冲突 ✅                │  │
│  │ 章节完整性: 10/10 ✅               │  │
│  │ 依赖声明: 已明示 ✅                │  │
│  └───────────────────────────────────┘  │
├─────────────────────────────────────────┤
│  📄 产物文件:                            │
│  [02-design-report.md] (11406B)         │
│  [02-gate-checks.yaml] (2925B)          │
│  [02-gate_check.py] (8636B)             │
│  [02-gate-result.md] ← 当前是 gate 结果  │
├─────────────────────────────────────────┤
│  🤖 自动检查: ✅ 7/7 通过                │
│  [查看明细]                              │
├─────────────────────────────────────────┤
│  操作:                                   │
│  [Approve] [Reject] [Force] [Waive]     │
│  理由（Reject/Force/Waive 必填）:        │
│  ┌───────────────────────────────────┐  │
│  │                                   │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### 8.3 操作与 API 的对应

Approve/Reject/Force/Waive 四个按钮 → 分别调用 §6.4 中的四个 API 端点。操作后页面 SSE 推送刷新。

---

## 9. API 端点扩展

### 9.1 现有端点（保持不动）

`POST /api/tasks` 等现有端点行为不变。单任务入口完整保留（Q9）。

### 9.2 新增端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/workflows` | 创建工作流实例。body: `{template_name, title, repo_path?}`。自动在 `workflow_runs` 建记录 + 创建第一个阶段的 task |
| `GET` | `/api/workflows` | 列出所有 workflow 实例（支持 ?status=running/completed） |
| `GET` | `/api/workflows/{id}` | 单个 workflow 详情 + 各阶段任务列表 + gate 状态 |
| `POST` | `/api/workflows/{id}/approve` | 批准当前阶段门控，流转下一阶段 |
| `POST` | `/api/workflows/{id}/reject` | 驳回，query `?reason=...`，当前阶段回到 queued |
| `POST` | `/api/workflows/{id}/force` | 强制流转，query `?reason=...` |
| `POST` | `/api/workflows/{id}/waive` | 豁免当前阶段，query `?reason=...` |
| `GET` | `/api/workflows/templates` | 列出已加载的 workflow 模板（name + description） |
| `GET` | `/api/workflows/{id}/stages` | 当前 workflow 的阶段列表 + task 状态 |
| `GET` | `/api/workflows/{id}/stage/{key}` | 特定阶段的详情 + gate 结果 |

---

## 10. 数据迁移策略

### 10.1 对现有数据的影响

- 现有 5 张表（tasks/comments/runtimes/daemon_state/agents）零修改
- 新增 `workflow_runs` 表
- `tasks` 表新增 5 列，全部 DEFAULT NULL → 现有行不受影响
- 新增 `pyyaml` 到 `requirements.txt`

### 10.2 迁移执行（daemon 启动时自动）

```python
def _migrate_v1_to_v2(conn):
    """v1 → v2: workflow 编排层迁移"""
    existing_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    
    if 'workflow_runs' not in existing_tables:
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_runs (... )""")
    
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for col, ddl in [
        ("workflow_run_id", "ALTER TABLE tasks ADD COLUMN workflow_run_id TEXT"),
        ("stage_key", "ALTER TABLE tasks ADD COLUMN stage_key TEXT"),
        ("stage_index", "ALTER TABLE tasks ADD COLUMN stage_index INTEGER"),
        ("gate_status", "ALTER TABLE tasks ADD COLUMN gate_status TEXT"),
        ("gate_result_json", "ALTER TABLE tasks ADD COLUMN gate_result_json TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(ddl)
```

`init_db()` 调用 `_migrate_v1_to_v2()`，复用现有 `_migrate_tasks()` 模式。首次启动自动执行，完全向后兼容现有 `orchestrator.db`。

### 10.3 回滚路径

迁移是纯增补（ADD COLUMN + CREATE TABLE）。如需回滚：删除 `workflow_runs` 表，tasks 的 5 个新列不影响查询。SQLite 不支持 DROP COLUMN（< 3.35），但在 3.35+ 可执行。降级方案：忽略新列即可，无功能影响。

---

## 11. 风险矩阵更新

### 11.1 Stage 1 风险的当前状态

| ID | Stage 1 风险 | 当前状态 | 变化 |
|----|-------------|---------|------|
| **R1** | pytest 未安装 | **修正**：pytest 框架未安装，但 `python tests/test_agent.py` 已跑通 49 项（unittest-style 直接执行）。Stage 4 安装 pytest 作为 QA 工具链一部分，非阻塞前置条件 | ↓ 降为中 |
| **R2** | sonar-scanner 未安装 | 已锁定降级方案：ruff + mypy + checkstyle JAR。SonarQube 评估为超需求 | ✅ 已解决 |
| **R3** | checkstyle 未安装 | 降级为 JAR 文件（无需安装，Java 17 已就绪）。Python 项目优先 ruff/mypy | ✅ 已解决 |
| R4 | Hermes bg 阻断 stdout | 无变化，workaround 延续（daemon 在真实终端运行）。不影响编排开发 | 无变化 |
| R5 | 长流程 token 累积 | pi compaction 配置足以应对 5 阶段串行。每阶段独立 session → 隔离 token 上下文 | 无变化 |
| R6 | DB 迁移风险 | 纯增补 DDL 设计（§3）→ 零数据风险 | ✅ 已缓解 |

### 11.2 新发现风险（Stage 2 特有）

| ID | 风险 | 严重度 | 缓解措施 | 验证方法 |
|----|------|--------|---------|---------|
| **R7** | 多阶段并发冲突 | **中** | 同一 workflow_run 的阶段串行执行（daemon 加 `WHERE stage_index = current_stage_index AND workflow_run_id = ?` 保证顺序）。不同 workflow 可并发 | Stage 5：同时创建 2 个 workflow，验证各自阶段交替执行不互相污染 |
| **R8** | 驳回后 resume session 的上下文污染 | **中** | 驳回时 task 回到 queued 复用 session_id，pi 继续同一 session → 上下文自然延续。如需完全重做，API 支持 `?reset=true` 新建 session | Stage 5：提交有瑕疵的设计 → reject → pi resume 修正 → 验证修正版不携带 reject 前的错误上下文 |
| **R9** | workflow 模板 YAML 语法错误导致运行期故障 | **低** | 模板在 daemon 启动时全部 `yaml.safe_load` 预加载，语法错误 → daemon 拒绝启动并打 err log。Stage 4 编写时可预先在 CI 检查 | Stage 4：构造含语法错误的 `bad.yaml`，daemon 启动 → 验证拒绝启动且 err log 包含 YAMLError 详情 |
| **R10** | gate.py YAML 规则解析失败导致门控静默通过 | **低** | gate.py 对 `yaml.safe_load` 异常做 FAIL 处理（非 PASS）。规则文件语法错误 = gate 不通过。同时 gate engine 在每次 check 最外层做 try/except → exception 也返回 FAIL | Stage 4：构造含非法 YAML 的规则文件，跑 gate.check() → 验证返回 FAIL 且 detail 含异常信息；检查 gate-result.md 不出现任何"PASS"标记 |

### 11.3 各阶段风险缓解动作（检查清单）

| 阶段 | 需验证的风险 | 具体动作 |
|------|------------|---------|
| Stage 3（实施计划） | R9/R10 预检 | 审查计划中"daemon 启动预加载模板"、"gate.py try/except"的实现描述是否完整 |
| Stage 4（实施） | R1 降级 / R9/R10 实现 | 安装 pytest + ruff + mypy；构造 bad.yaml 和非法规则文件验证拒绝行为；测试 gate engine 异常路径 |
| Stage 5（QA） | R7/R8 / R3 闭环 | 创建 2 个并发 workflow 验证交替执行；提交瑕疵设计 → reject → resume 修正验证；wf-002 自举验证编排器闭环 |

---

## 12. 后续阶段交付物定义

### Stage 3 实施计划

基于本设计报告，分解为 bite-sized 任务（每个 2-5 分钟），含：
1. DB 迁移：`database.py` 新增表 + 字段 + `_migrate_v1_to_v2()`
2. `workflow.py`：模板加载 + 阶段流转 + 上下文注入
3. `gate.py`：YAML 驱动通用门控引擎 + `yaml_parse` 类型
4. `qa.py`：ruff/mypy/checkstyle 子进程调用 + 报告格式化
5. `daemon.py`：新增 workflow 轮询逻辑
6. `server.py`：新增 10 个 API 端点 + 审批 UI 面板
7. 模板：`workflows/default.yaml` + `workflows/minimal.yaml`
8. 测试：`test_workflow.py` + `test_gate.py` + `test_qa.py`

### Stage 5 QA（wf-001 自身）

1. 跑完整 `tests/` 套件（含新增测试）
2. ruff + mypy 扫描全部新增 `.py` 文件
3. **wf-002 自举验证**：用编排器自身管理一个最小二阶段 workflow → 验证闭环（解决 Stage 1 缺陷 #3）

---

## 13. 附录：环境状态快照（2026-08-01）

| 项目 | 状态 |
|------|------|
| Python | 3.11.15 |
| PyYAML | 6.0.3 ✅ |
| pi CLI | `pi.CMD` detected (`C:\Users\hy931\AppData\Roaming\npm\pi.CMD`) |
| Java | OpenJDK 17.0.18 Temurin ✅ |
| pytest | 未安装（unittest-style 可用） |
| sonar-scanner | 未安装（降级为 ruff+mypy+checkstyle JAR） |
| checkstyle | 未安装（降级为 JAR 文件） |
| Git | 2.47.1 ✅ |
| curl | 8.18.0 ✅ |
| ruff | 未安装（将加入 requirements.txt） |
| mypy | 未安装（将加入 requirements.txt） |
| 现有单测 | test_agent.py 49/49 ✅；test_e2e.py 16 项（`grep -c '^\s*check(' tests/test_e2e.py` = 16）。test_e2e.py 需全链路环境（daemon + pi）方可运行 |
| 现有 DB | orchestrator.db 含 5 表，WAL 模式 |
