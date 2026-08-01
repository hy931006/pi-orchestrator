# 可行性分析报告

> **工作流**: wf-001 | **阶段**: 01-feasibility | **日期**: 2026-08-01
> **基线 commit**: `8efdf07`（pi-orchestrator v0 全量导入）

---

## 1. 现状分析

### 1.1 现有资产：pi-orchestrator v0

`Documents/pi-orchestrator/` 已有一套功能完整的 **任务级执行引擎**：

| 组件 | 行数 | 核心能力 |
|------|------|---------|
| `agent.py` | 689 | PiAgent — 像素级复刻 Multica `pi.go`：JSON 事件流解析、thinking 提取、ANSI 清洗、超时/取消看门狗、Windows `.cmd`→`.ps1` 重写 |
| `daemon.py` | 365 | 任务调度器：sqlite 轮询→原子认领→ThreadPool 真并发（MAX=3）→Git Worktree 隔离→终态写入 |
| `database.py` | 463 | SQLite（WAL 模式）+ 5 表：tasks/runtimes/agents/comments/daemon_state + 列迁移 + 僵尸任务恢复 |
| `server.py` | 242 | FastAPI REST 接口 + SSE 实时推送 + Web UI 仪表盘（Dark 主题） |
| `tests/` | 3 文件 | 49 项 agent 单元测试（已在本环境通过，`python tests/test_agent.py` → 49 通过/0 失败）。E2E 测试（test_e2e.py, 16 项）因需 daemon+pi+sqlite 全链路运行环境，未在本可行性分析阶段验证 |

任务状态机：`queued → claimed → running → completed/failed/blocked/cancelled`

### 1.2 能力缺口

当前模型是**「一个任务 → 一个 pi 进程」的平面调度**。需要升级为**「一个工作流 → N 个阶段 → 每阶段独立 pi 子任务 → 门控流转」的纵向编排**。具体差距：

| 缺失能力 | 说明 |
|---------|------|
| 工作流实例 | 无 `workflows` 表/模型，无法表达「分析→设计→计划→实施→QA」的五阶段流水线及其顺序 |
| 阶段定义 | tasks 表无 `phase`/`stage` 字段，无法将 pi 子任务归属到特定阶段 |
| 门控机制 | 无 gate rule 定义、无自动检查执行器、无人工审批语义 |
| 阶段上下文传递 | 上游产物无法自动注入下游 pi 子任务的 prompt |
| 产物追溯 | 无文件系统 + git commit 的产物存储约定 |
| 人工审批 UI | Web UI 无审批操作入口 |

**结论**：基础设施（agent 调用、调度、存储、Web UI）已就绪，缺失的是**编排维度的数据模型 + 门控规则引擎**。扩展代价可控。

---

## 2. 需求分析

以下为用户 2026-08-01 确认的需求基线（Q1-Q10），逐条记录。

### 2.1 流程定义

| 问题 | 用户决定 |
|------|---------|
| **Q1** 阶段模板可配置 vs 硬编码？ | **可配置**。不同工作流可定义不同阶段序列（如简单修复可跳过设计阶段） |
| **Q2** 阶段内执行模型？ | **独立 pi 子任务**。每个阶段派发独立子任务执行→产出阶段产物→门控判定→流转。不采用单次 pi 调用内自行流转 |

### 2.2 门控规则

| 问题 | 用户决定 |
|------|---------|
| **Q3** 门控判定方式？ | **C（混合）**：机器自动检查（文档存在、结构完整、代码质量指标）先行，通过后进入人工批复 |
| **Q4** 门控失败语义？ | 给人工提供可选项（回退重做 / 强制流转 / 标记豁免并记录原因） |
| **Q5** 自动校验覆盖范围？ | 文档结构完整性、文档正确性/合理性（合理性属**人工兜底维度**，机器仅做结构性检查）、代码质量（lint 扫描）、代码实现完整性（需求覆盖率） |

### 2.3 文档追溯

| 问题 | 用户决定 |
|------|---------|
| **Q6** 产物存储方案？ | **A+B 无 DB 索引**：git 分支内提交 + `docs/<workflow-id>/<stage-id>/` 文件系统。产物文档靠 git 追溯 + 文件系统，**不建额外的 DB 索引**。编排状态（工作流实例、阶段状态、gate 结果）**仍需存 DB**，因为 Web UI 实时显示依赖它且 pi-orchestrator 本身就是 DB 驱动的。 |

### 2.4 QA 阶段

| 问题 | 用户决定 |
|------|---------|
| **Q7** QA 阶段定义？ | 自动测试套件 + sonar/checkstyle 代码扫描 + 自定义规则。报告需含：问题内容、位置（文件:行号）、严重程度（blocker/critical/major/minor）、是否阻断、其他字段自行扩展 |

### 2.5 范围与兼容

| 问题 | 用户决定 |
|------|---------|
| **Q8** 新编排层与现有 pi-orchestrator 的关系？ | **扩展**（在现有代码库内增量开发，非重写） |
| **Q9** 现有单任务入口是否保留？ | **可以**。编排层上线后，独立单任务（不经过五阶段流程）仍可继续提交。编排器需**两种入口共存** |
| **Q10** 本 meta 项目自身走五阶段流程？ | **是**。当前交付物即 wf-001 的 Stage 1 gate artifact |

---

## 3. 技术方案

### 3.1 方案对比

| 方案 | 描述 | 优势 | 劣势 | 推荐 |
|------|------|------|------|------|
| **A. 扩展 pi-orchestrator** | 新增 2-3 个 Python 模块：`workflow.py`（工作流引擎）、`gate.py`（门控规则引擎）、`qa.py`（QA 扫描集成）+ 增补 tasks 表/add workflows 表 | 零新依赖、复用现有 agent/daemon/db/server、部署方式不变 | 需要谨慎设计 DB 迁移策略 | ✅ **推荐** |
| **B. 独立编排器** | 新建独立项目，通过 pi-orchestrator REST API 调度子任务 | 代码隔离清晰 | 两套系统耦合调试困难、API 版本管理成本、违背"扩展"需求 | ❌ |
| **C. 引入 Prefect/Temporal** | 外部工作流引擎 | 成熟度高 | 重量级外部依赖、内网 air-gapped 可能不可用、大炮打蚊子 | ❌ |

### 3.2 推荐方案：扩展 pi-orchestrator

**核心架构**（高层次，细节留给 Stage 2 详细设计）：

```
                    ┌──────────────────────────────────┐
                    │  workflow.py（编排引擎）          │
                    │  ├── workflow templates (YAML)    │
                    │  ├── 实例生命周期管理             │
                    │  └── 阶段流转 + 上下文传递        │
                    └──────────┬───────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │  gate.py     │  │  qa.py       │  │  agent.py    │
    │  门控规则引擎 │  │  QA 扫描集成  │  │  Pi 适配器   │
    │  auto+human  │  │  sonar/chkl  │  │  (已有)      │
    └──────────────┘  └──────────────┘  └──────────────┘
```

**执行模型**（Q2 独立子任务）：

1. workflow 实例启动 → 当前阶段 `queued`
2. daemon 轮询到该阶段 → 构造 prompt（含上游产物 + 阶段指令）→ 派发 pi 子进程
3. pi 产出文本 + 工具调用的文件修改 → daemon 保存产物到 `docs/<wf-id>/<stage-id>/`
4. daemon 标记阶段 `running→completed` → 触发 gate 检查
5. gate 自动检查通过 → 人工审批 → 通过后下一阶段 `queued` 或 workflow `completed`

**存储约定**（Q6）：

- 编排元数据：SQLite（workflows 表 + tasks 表新增 `workflow_id`/`stage`/`stage_index`/`gate_status` 字段）
- 阶段产物：git repo 内 `docs/<workflow-id>/<stage-id>/` 文件系统。每个阶段 commit 独立追溯
- 产物索引：git log + 文件系统 `tree`，**不建 DB 索引**

**两种入口共存**（Q9）：

- `POST /api/tasks` — 单任务入口（现有，不受影响）
- `POST /api/workflows` — 工作流入库（新，自动分解为 N 个阶段子任务）

---

## 4. 风险矩阵

### 4.1 环境缺失风险（经 2026-08-01 探测验证）

| ID | 风险 | 当前状态 | 严重度 | 缓解措施 |
|----|------|---------|--------|---------|
| **R1** | **pytest 未安装** | `ModuleNotFoundError: No module named 'pytest'`；现有测试用 `python tests/test_*.py` 直接跑 | **高** | Stage 4 前安装 pytest 或编写轻量 test runner（`python -m unittest` 已内置） |
| **R2** | **sonar-scanner 未安装** | `where sonar-scanner` 无结果 | **高** | Stage 2 详细设计时确定降级方案：① checkstyle 单 jar + 自定义规则 ② pylint/ruff 替代 Python 静态分析 ③ SonarQube 是否可在内网部署 |
| **R3** | **checkstyle 未安装** | `where checkstyle` 无结果 | **高** | 同 R2。Python 项目用 ruff/mypy/pylint 替代；Java 项目（用户背景）需 checkstyle jar 文件 |
| **R4** | **Hermes background 阻断子进程 stdout** | pi-orchestrator 已记录：daemon 必须在真实终端运行 | **中** | 已有 workaround（独立终端窗口）。不影响编排层开发，但影响自动化部署 |
| **R5** | **多阶段长流程 token 累积** | 5 阶段 × 每阶段可能数十轮对话 | **中** | pi compaction 配置可用：`reserveTokens=16384` + `keepRecentTokens=65536` |
| **R6** | **DB 迁移风险** | 现有 orchestrator.db 含 5 表 + 3 次列迁移 | **低** | SQLite 列迁移已有成熟模式（`PRAGMA table_info` 检查后 ALTER），编排层新增表不影响现有数据 |

### 4.2 设计层面风险

| ID | 风险 | 严重度 | 缓解措施 |
|----|------|--------|---------|
| **R7** | Q5「合理性」维度定义模糊 | **中** | 明确：合理性为**人工兜底维度**，机器仅做结构性检查（文档章节齐全、代码 lint 通过、测试覆盖率≥阈值）。合理性判断留存人工审批 |
| **R8** | 人工审批无 SLA/超时机制 | **低** | 标记为 Stage 2 延期决策——当前阶段不做约束，仅记录 |
| **R9** | self-bootstrap：用未完成的编排器管理编排器的开发 | **低** | wf-001 由人工（你 + 我）直接执行流程，不依赖编排器自身。这是正常的自举模式 |

---

## 5. 结论

### 5.1 可行性判定

**有条件通过（GO with conditions）**。
技术可行性高——现有 pi-orchestrator 提供了 80% 的基础设施，编排层扩展代价可控（预估新增 3 个模块 + 1-2 张表 + 1 个 YAML schema）。

三个条件（对应环境缺失风险 R1-R3）：
1. **pytest** 须在 Stage 4 前安装或采用替代测试 runner
2. **sonar-scanner / checkstyle** 须在 Stage 2 详细设计阶段确定降级方案
3. QA 阶段自动扫描能力须在工具链确认后定义具体 check 规则

### 5.2 延期决策清单（移交 Stage 2）

以下问题**不在 Stage 1 内决策**，需在 Stage 2 详细设计时解决：

1. workflow 模板 YAML schema 定义（阶段列表、每阶段 agent 配置、门控规则引用）
2. 人工审批 UI 形态（Web 页面内嵌 / 独立面板 / 仅 API）
3. DB 具体迁移方案（新表 DDL + 旧表字段补充）
4. 阶段上下文传递的具体 prompt 模板
5. checkstyle/sonar 降级方案的最终选型
6. 人工审批超时/回退/SLA 策略
7. Q5「合理性」维度的详细判定标准

### 5.3 下一步

本报告 + gate-checks.yaml + gate-result.md 构成 Stage 1 完整交付物。
用户审批 gate-result.md 中 3 项人工检查（H1/H2/H3）通过后，进入 Stage 2「详细设计」。
