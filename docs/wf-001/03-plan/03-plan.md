# 实施计划 — Pi 编排调度层

> **工作流**: wf-001 | **阶段**: 03-plan | **日期**: 2026-08-02
> **上游**: 02-design-report.md（Stage 2 gate 已闭合，commit `7509cc6`）
> **执行模式**: 用户授权代理决策（2026-08-01），所有 H 项由 Agent 代为决策并记录，无人工审批环节。
> **给 Stage 4 实施者**: 逐任务执行，每任务含精确文件路径、完整代码/命令、验收标准。TDD 优先。

---

## 1. 任务总览

| # | 任务 | 文件 | 依赖 | 预估耗时 |
|---|------|------|------|---------|
| T1 | DB 迁移：workflow_runs 表 + tasks 5 列 | `database.py` | — | 15 min |
| T2 | workflow.py：模板加载 + 流转 + 上下文注入 | `workflow.py`（新建） | T1 | 30 min |
| T3 | gate.py：YAML 驱动门控引擎 | `gate.py`（新建） | — | 30 min |
| T4 | qa.py：静态扫描 + 报告生成 | `qa.py`（新建） | — | 20 min |
| T5 | daemon.py：workflow 轮询接入 | `daemon.py` | T1/T2 | 15 min |
| T6 | server.py：10 个 API 端点 | `server.py` | T1/T2 | 20 min |
| T7 | 模板：default.yaml + minimal.yaml | `workflows/`（新建） | T2 | 10 min |
| T8 | 测试：test_workflow/test_gate/test_qa | `tests/` | T2-T4 | 25 min |
| T9 | 全量回归：单测 + 现有 E2E | `tests/` | T8 | 10 min |

**实施顺序**：T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9（严格串行，每任务完成即 commit）

---

## 2. T1: DB 迁移

**目标**：新增 `workflow_runs` 表 + tasks 表 5 列，纯增补迁移。

**文件**: `database.py`

**步骤 1**: 在 `init_db()` 的 `executescript` 后追加调用 `_migrate_v1_to_v2(conn)`，并在文件末尾新增：

```python
def _migrate_v1_to_v2(conn):
    """v1 → v2: workflow 编排层迁移（纯增补，零数据风险）"""
    existing_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "workflow_runs" not in existing_tables:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                template_name TEXT NOT NULL,
                title TEXT NOT NULL,
                repo_path TEXT,
                branch TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                current_stage TEXT,
                current_stage_index INTEGER DEFAULT 0,
                artifact_dir TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
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

**步骤 2**: 新增 workflow_runs CRUD：

```python
def create_workflow_run(template_name, title, repo_path=None) -> dict:
    """创建 workflow 实例"""
    run_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO workflow_runs (id, template_name, title, repo_path, artifact_dir) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, template_name, title, repo_path or str(Path.cwd()),
             f"docs/{run_id}"))
    return get_workflow_run(run_id)

def get_workflow_run(run_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None

def list_workflow_runs(status: str = None, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM workflow_runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

def update_workflow_run(run_id: str, **kwargs):
    allowed = {"status", "current_stage", "current_stage_index", "branch"}
    fields, values = [], []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            values.append(v)
    if not fields:
        return
    fields.append("updated_at = datetime('now','localtime')")
    values.append(run_id)
    with get_db() as conn:
        conn.execute(f"UPDATE workflow_runs SET {', '.join(fields)} WHERE id = ?", values)
```

**验收**: `python -c "import database; database.init_db(); print(database.get_workflow_run.__doc__)"` 无异常；`PRAGMA table_info(tasks)` 含 5 个新列。

---

## 3. T2: workflow.py

**目标**：模板加载 + 阶段流转 + 上下文注入。

**文件**: `workflow.py`（新建）

**核心实现**（完整代码）：

```python
"""workflow.py — 工作流编排引擎"""
import logging
import subprocess
import sys
from pathlib import Path

import yaml

import database as db

logger = logging.getLogger("workflow")

WORKFLOWS_DIR = Path(__file__).parent / "workflows"


class WorkflowError(Exception):
    pass


def load_templates() -> dict:
    """扫描 workflows/*.yaml，加载为 {name: template}。语法错误 → 拒绝启动"""
    templates = {}
    if not WORKFLOWS_DIR.exists():
        return templates
    for f in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            templates[data["name"]] = data
        except (yaml.YAMLError, KeyError) as e:
            raise WorkflowError(f"模板 {f.name} 解析失败: {e}")
    return templates


def validate_template(template: dict) -> None:
    """创建时的 schema 校验（对应设计 §4.4）"""
    if not template.get("name"):
        raise WorkflowError("模板缺少 name")
    stages = template.get("stages")
    if not stages or not isinstance(stages, list):
        raise WorkflowError("模板 stages 为空或非列表")
    indices = [s.get("index") for s in stages]
    if indices != list(range(len(stages))):
        raise WorkflowError(f"阶段 index 必须连续 0..{len(stages)-1}: {indices}")
    for s in stages:
        if not s.get("required_artifacts"):
            raise WorkflowError(f"阶段 {s.get('key')} 缺少 required_artifacts")


def inject_context(stage: dict, artifact_dir: str) -> str:
    """构造包含上游产物的 prompt 前缀（设计 §5.2）"""
    lines = [
        f"# 阶段: {stage.get('label', stage['key'])} ({stage['key']})",
        f"# 产物目录: {artifact_dir}",
        "",
    ]
    prior = stage.get("context_inject", {}).get("prior_stages", [])
    if prior:
        lines.append("## 上游阶段产出（请阅读后基于其决策工作）")
        for sk in prior:
            lines.append(f"- {sk}: {artifact_dir}/{sk}/")
            lines.append(f"  请读取该目录下的报告文件，理解其结论和约束")
        lines.append("")
    lines.append("## 本阶段任务")
    lines.append(stage["agent"].get("system_prompt", ""))
    return "\n".join(lines)


def create_workflow(template_name: str, title: str, repo_path: str = None) -> dict:
    """创建 workflow 实例 + 第一个阶段任务（设计 §9.2 POST /api/workflows）"""
    templates = load_templates()
    if template_name not in templates:
        raise WorkflowError(f"未知模板: {template_name}，可用: {list(templates)}")
    template = templates[template_name]
    validate_template(template)

    run = db.create_workflow_run(template_name, title, repo_path)
    stages = template["stages"]
    first = stages[0]

    # 创建第一阶段任务
    task = db.create_task(
        title=f"[{run['id']}] {first['label']}",
        description=inject_context(first, run["artifact_dir"]),
        agent_type="auto",
        repo_path=run["repo_path"],
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET workflow_run_id=?, stage_key=?, stage_index=?, gate_status='pending' WHERE id=?",
            (run["id"], first["key"], first["index"], task["id"]))
        conn.execute(
            "UPDATE workflow_runs SET current_stage=?, current_stage_index=? WHERE id=?",
            (first["key"], first["index"], run["id"]))
    logger.info(f"🆕 workflow {run['id']} 创建: {template_name} → stage {first['key']}")
    return {"run": run, "first_task": task, "template": template}


def advance_stage(run_id: str) -> Optional[dict]:
    """当前阶段完成后推进到下一阶段（幂等，乐观锁）"""
    run = db.get_workflow_run(run_id)
    if not run:
        raise WorkflowError(f"workflow 不存在: {run_id}")
    templates = load_templates()
    template = templates[run["template_name"]]
    stages = template["stages"]
    next_idx = run["current_stage_index"] + 1

    if next_idx >= len(stages):
        db.update_workflow_run(run_id, status="completed")
        logger.info(f"✅ workflow {run_id} 全部阶段完成")
        return None

    stage = stages[next_idx]
    task = db.create_task(
        title=f"[{run_id}] {stage['label']}",
        description=inject_context(stage, run["artifact_dir"]),
        agent_type="auto",
        repo_path=run["repo_path"],
    )
    with db.get_db() as conn:
        # 乐观锁：仅当仍指向上一阶段时推进
        cur = conn.execute(
            "UPDATE workflow_runs SET current_stage=?, current_stage_index=? "
            "WHERE id=? AND current_stage_index=?",
            (stage["key"], next_idx, run_id, run["current_stage_index"])).rowcount
        if cur == 0:
            return None  # 并发竞争，丢弃本次
        conn.execute(
            "UPDATE tasks SET workflow_run_id=?, stage_key=?, stage_index=?, gate_status='pending' WHERE id=?",
            (run_id, stage["key"], next_idx, task["id"]))
    logger.info(f"➡️ workflow {run_id} → stage {stage['key']}")
    return task
```

**验收**: `python -c "import workflow; print(workflow.load_templates())"` 输出模板 dict（T7 后非空）；`workflow.validate_template` 对缺 index 的模板抛 WorkflowError。

---

## 4. T3: gate.py

**目标**：通用 YAML 驱动门控引擎（修复 Stage 1 硬编码缺陷）。

**文件**: `gate.py`（新建）

**核心实现**：

```python
"""gate.py — 通用门控引擎（YAML 驱动，零硬编码）"""
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

logger = logging.getLogger("gate")


class GateError(Exception):
    pass


class GateEngine:
    """通用门控引擎 — 规则文件驱动"""

    def __init__(self, rules_path: Path, artifact_dir: Path):
        if not rules_path.exists():
            raise GateError(f"规则文件不存在: {rules_path}")
        try:
            self.rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise GateError(f"规则文件 YAML 解析失败（R10: 解析失败即 FAIL 而非 PASS）: {e}")
        self.artifact_dir = artifact_dir
        self.report_text = ""
        # 找到目标报告文件（按 target glob 匹配 artifact_dir 下最近修改的文件）
        self._resolve_targets()

    def _resolve_targets(self):
        self._target_files = {}
        for check in self.rules.get("checks", []):
            tgt = check.get("target", "")
            if tgt and tgt != "人工":
                matches = sorted(self.artifact_dir.glob(tgt), key=lambda p: p.stat().st_mtime, reverse=True)
                if matches:
                    self._target_files[check["id"]] = matches[0]
        # 报告文本 = 第一个 structure 型 target 的内容
        for check in self.rules.get("checks", []):
            if check.get("type") == "structure" and check["id"] in self._target_files:
                self.report_text = self._target_files[check["id"]].read_text(encoding="utf-8", errors="replace")
                break

    def run(self) -> list[dict]:
        results = []
        for check in self.rules.get("checks", []):
            try:
                handler = {
                    "structure": self._check_structure,
                    "content": self._check_content,
                    "cross_ref": self._check_cross_ref,
                    "yaml_parse": self._check_yaml_parse,
                    "human": self._check_human,
                }.get(check.get("type"))
                results.append(handler(check) if handler else {
                    "id": check["id"], "severity": "blocker", "passed": False,
                    "detail": f"未知检查类型: {check.get('type')}"})
            except Exception as e:
                # R10: 任何异常 → FAIL 而非静默 PASS
                results.append({"id": check["id"], "severity": "blocker", "passed": False,
                                "detail": f"检查异常（按 R10 处理为 FAIL）: {e}"})
        return results

    def _check_structure(self, check: dict) -> dict:
        cid = check["id"]
        f = self._target_files.get(cid)
        if not f:
            return {"id": cid, "severity": check["severity"], "type": "structure",
                    "rule": check["rule"], "passed": False, "detail": "target 文件不存在"}
        expected = check.get("expected_sections", [])
        text = f.read_text(encoding="utf-8", errors="replace")
        missing = [s for s in expected if s not in text]
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else f"all {len(expected)} present"}

    def _check_content(self, check: dict) -> dict:
        cid = check["id"]
        text = self.report_text
        if not text:
            return {"id": cid, "severity": check["severity"], "type": "content",
                    "rule": check["rule"], "passed": False, "detail": "无报告文本"}
        placeholders = [r'\bTODO\b', r'\bTBD\b', r'\bFIXME\b', r'待定']
        found = [p for pat in placeholders for p in re.findall(pat, text)]
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": len(found) == 0,
                "detail": f"found={found}" if found else "clean"}

    def _check_cross_ref(self, check: dict) -> dict:
        cid = check["id"]
        tokens = check.get("expected_tokens", [])
        text = self.report_text
        missing = [t for t in tokens if t not in text]
        return {"id": cid, "severity": check["severity"], "type": "cross_ref",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else f"all {len(tokens)} present"}

    def _check_yaml_parse(self, check: dict) -> dict:
        cid = check["id"]
        idx = check.get("yaml_block_index", 0)
        blocks = re.findall(r'```yaml\n(.*?)```', self.report_text, re.DOTALL)
        if idx >= len(blocks):
            return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                    "rule": check["rule"], "passed": False,
                    "detail": f"yaml block {idx} 超出范围（共 {len(blocks)} 个）"}
        try:
            yaml.safe_load(blocks[idx])
            return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                    "rule": check["rule"], "passed": True,
                    "detail": f"block[{idx}] valid YAML"}
        except yaml.YAMLError as e:
            return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                    "rule": check["rule"], "passed": False,
                    "detail": f"block[{idx}] YAML error: {e}"}

    def _check_human(self, check: dict) -> dict:
        """人审项：预检 + 占位。决策由外部（审批 API / 决策文件）写入"""
        cid = check["id"]
        precheck = {t: t in self.report_text for t in check.get("machine_checks", [])}
        return {"id": cid, "severity": check["severity"], "type": "human",
                "rule": check["rule"], "passed": None,
                "detail": f"precheck: {sum(1 for v in precheck.values() if v)}/{len(precheck)}",
                "human_review_decision": None, "human_reviewer": None,
                "human_review_at": None}

    def generate_markdown(self, results: list[dict]) -> str:
        """输出 gate-result.md（结构对齐 02-gate-result.md）"""
        machine = [r for r in results if r.get("type") != "human"]
        blockers = [r for r in machine if r["severity"] == "blocker" and r["passed"] is False]
        overall = "PASS" if not blockers else "FAIL"
        lines = [
            f"# Gate Check Result — {self.artifact_dir.name}",
            "",
            "## 总览",
            "",
            f"| 维度 | 结果 |",
            f"|------|------|",
            f"| 自动检查 | **{overall}** |",
            f"| 人工审批 | **PENDING** |",
            "",
            "## 自动检查明细",
            "",
            "| ID | 严重度 | 类型 | 规则 | 结果 | 详情 |",
            "|----|--------|------|------|------|------|",
        ]
        for r in machine:
            icon = "✅" if r["passed"] else "❌"
            lines.append(f"| {r['id']} | {r['severity']} | {r['type']} | {r['rule']} | {icon} | {r['detail']} |")
        lines += ["", "## 人工审批", "", "| ID | 规则 | 预检 | 审批决定 |", "|----|------|------|---------|"]
        for r in [x for x in results if x.get("type") == "human"]:
            lines.append(f"| {r['id']} | {r['rule'][:60]}... | {r['detail']} | `<待填写>` |")
        return "\n".join(lines) + "\n"


def run_gate(rules_path: Path, artifact_dir: Path) -> tuple[bool, str]:
    """执行门控，返回 (是否通过, gate-result.md 内容)"""
    engine = GateEngine(rules_path, artifact_dir)
    results = engine.run()
    md = engine.generate_markdown(results)
    result_file = artifact_dir / "gate-result.md"
    result_file.write_text(md, encoding="utf-8")
    machine = [r for r in results if r.get("type") != "human"]
    blockers = [r for r in machine if r["severity"] == "blocker" and r["passed"] is False]
    return (len(blockers) == 0, md)
```

**验收**: `python -c "import gate; print(gate.GateEngine)"` 无异常；对坏规则文件（含非法 YAML）构造 GateEngine 应抛 GateError。

---

## 5. T4: qa.py

**目标**: ruff/mypy/checkstyle 子进程调用 + QA 报告生成。

**文件**: `qa.py`（新建）

**核心实现**：

```python
"""qa.py — QA 扫描集成（ruff/mypy/checkstyle + 自定义规则）"""
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

logger = logging.getLogger("qa")

SEVERITY_BLOCKING = {"blocker", "critical"}


def run_ruff(paths: list[Path]) -> list[dict]:
    """ruff check → 发现列表。ruff 未安装时返回空并打 warning"""
    findings = []
    ruff = shutil.which("ruff")
    if not ruff:
        logger.warning("ruff 未安装，跳过 lint（Stage 4 安装后生效）")
        return findings
    try:
        out = subprocess.run([ruff, "check", "--output-format", "json"] + [str(p) for p in paths],
                             capture_output=True, text=True, timeout=120)
        import json
        for item in json.loads(out.stdout or "[]"):
            findings.append({
                "tool": "ruff", "file": item.get("filename", ""),
                "line": item.get("location", {}).get("row", 0),
                "col": item.get("location", {}).get("column", 0),
                "code": item.get("code", ""), "severity": "error",
                "blocking": True, "message": item.get("message", ""),
            })
    except Exception as e:
        logger.warning(f"ruff 执行异常: {e}")
    return findings


def run_mypy(paths: list[Path]) -> list[dict]:
    """mypy → 发现列表。未安装时跳过"""
    findings = []
    mypy = shutil.which("mypy")
    if not mypy:
        logger.warning("mypy 未安装，跳过 type check")
        return findings
    try:
        out = subprocess.run([mypy, "--no-error-summary"] + [str(p) for p in paths],
                             capture_output=True, text=True, timeout=120)
        for line in (out.stdout or "").splitlines():
            m = re.match(r"(.+?):(\d+):(?:(\d+):)?\s*(error|note):\s*(.+)", line)
            if m and m.group(4) == "error":
                findings.append({
                    "tool": "mypy", "file": m.group(1), "line": int(m.group(2)),
                    "col": int(m.group(3) or 0), "code": "", "severity": "error",
                    "blocking": False, "message": m.group(5),
                })
    except Exception as e:
        logger.warning(f"mypy 执行异常: {e}")
    return findings


def run_custom_rules(paths: list[Path], rules_file: Path) -> list[dict]:
    """自定义 grep 规则扫描（设计 §7.3）"""
    findings = []
    if not rules_file.exists():
        return findings
    rules = yaml.safe_load(rules_file.read_text(encoding="utf-8")).get("rules", [])
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for rule in rules:
            glob_ok = True
            if rule.get("file_glob"):
                import fnmatch
                glob_ok = fnmatch.fnmatch(path.name, rule["file_glob"])
            if not glob_ok:
                continue
            for m in re.finditer(rule["pattern"], text):
                line_no = text[:m.start()].count("\n") + 1
                findings.append({
                    "tool": "custom", "file": str(path), "line": line_no,
                    "col": 0, "code": rule["id"],
                    "severity": rule.get("severity", "warning"),
                    "blocking": rule.get("blocking", False),
                    "message": rule.get("description", ""),
                })
    return findings


def generate_qa_report(paths: list[Path], test_summary: dict = None) -> str:
    """生成 QA 报告 markdown（设计 §7.2 四章节结构）"""
    ruff_f = run_ruff(paths)
    mypy_f = run_mypy(paths)
    rules_f = run_custom_rules(paths, Path(__file__).parent / "qa-rules.yaml")
    all_f = ruff_f + mypy_f + rules_f
    blockers = [f for f in all_f if f["blocking"]]
    warnings = [f for f in all_f if not f["blocking"]]

    lines = [
        "# QA Report",
        "",
        "## 1. 测试套件",
        "",
        "| 测试文件 | 通过 | 失败 | 跳过 | 耗时 |",
        "|---------|------|------|------|------|",
    ]
    if test_summary:
        for name, s in test_summary.items():
            lines.append(f"| {name} | {s.get('passed', 0)} | {s.get('failed', 0)} | {s.get('skipped', 0)} | {s.get('duration', 0):.1f}s |")
    lines += [
        "",
        "## 2. 静态扫描",
        "",
        "### ruff",
        "| 文件 | 行号 | 规则 | 严重度 | 内容 |",
        "|------|------|------|--------|------|",
    ]
    for f in ruff_f:
        lines.append(f"| {f['file']} | {f['line']} | {f['code']} | {f['severity']} | {f['message']} |")
    lines += ["", "### mypy", "| 文件 | 行号 | 严重度 | 内容 |", "|------|------|--------|------|"]
    for f in mypy_f:
        lines.append(f"| {f['file']} | {f['line']} | {f['severity']} | {f['message']} |")
    lines += [
        "",
        "## 3. 自定义规则扫描",
        "| 规则ID | 文件 | 位置 | 严重度 | 是否阻断 | 描述 |",
        "|--------|------|------|--------|---------|------|",
    ]
    for f in rules_f:
        lines.append(f"| {f['code']} | {f['file']} | {f['line']} | {f['severity']} | {f['blocking']} | {f['message']} |")
    lines += [
        "",
        "## 4. 阻断总结",
        f"- 阻断项: {len(blockers)}",
        f"- 警告项: {len(warnings)}",
        f"- **最终判定**: {'❌ BLOCKED' if blockers else '✅ PASS'}",
        "",
    ]
    return "\n".join(lines) + "\n"
```

**验收**: `python -c "import qa; print(qa.generate_qa_report.__doc__)"` 无异常；无 ruff/mypy 时返回空扫描章节不崩溃。

---

## 6. T5: daemon.py 接入 workflow

**目标**: daemon 轮询时同时处理 workflow 阶段任务，阶段完成后触发 gate + 自动流转。

**文件**: `daemon.py`

**修改点**:

1. `import workflow as wf` + `import gate`
2. `main()` 中 `db.init_db()` 后调用 `wf.load_templates()` 预加载（R9 缓解：失败即拒绝启动）
3. `TaskExecutor._execute_inner` 中，任务完成后（status=completed 分支），检查 `task.get("workflow_run_id")`：
   - 有 → 调用 `_after_stage(task)`：跑 gate（`gate.run_gate(rules_path, artifact_dir)`），写 `gate_status` + `gate_result_json`，然后 `wf.advance_stage(run_id)`
4. 新增方法：

```python
def _after_stage(self, task: dict):
    """workflow 阶段任务完成后的门控 + 流转"""
    run_id = task.get("workflow_run_id")
    if not run_id:
        return
    run = db.get_workflow_run(run_id)
    if not run:
        return
    artifact_dir = Path(run["artifact_dir"])
    # 找该阶段的 gate 规则文件
    templates = wf.load_templates()
    template = templates.get(run["template_name"])
    if not template:
        logger.error(f"workflow {run_id}: 模板 {run['template_name']} 丢失")
        return
    stages = template["stages"]
    stage = stages[task.get("stage_index") or 0]
    rules_rel = stage.get("gate_rules", "")
    if rules_rel:
        rules_path = artifact_dir / rules_rel
        passed, md = gate.run_gate(rules_path, artifact_dir / task["stage_key"])
        gate_status = "auto_passed" if passed else "auto_failed"
        db.update_task_status(task["id"], "completed",
                              gate_status=gate_status,
                              gate_result_json=json.dumps({"passed": passed}))
        logger.info(f"🔒 [{task['id']}] gate={gate_status} ({'✅' if passed else '❌'})")
    # 自动流转（代理决策模式：auto_passed 直接推进，auto_failed 推进并记录）
    wf.advance_stage(run_id)
```

> 注: `db.update_task_status` 需扩展允许 `gate_status`/`gate_result_json` 字段（T1 已加列，此处需在 `update_task_status` 的 kwargs 白名单中补充）。

**验收**: `python -c "import daemon"` 无语法错误；mock 环境下 workflow 阶段完成后 task 出现 `gate_status=auto_passed`。

---

## 7. T6: server.py 新增端点

**目标**: 10 个 workflow API 端点（设计 §9.2）。

**文件**: `server.py`

**新增**（追加到文件末尾，`if __name__` 之前）：

```python
# ── Workflows ──

class CreateWorkflowRequest(BaseModel):
    template_name: str
    title: str
    repo_path: str = ""


@app.post("/api/workflows")
async def create_workflow(req: CreateWorkflowRequest):
    try:
        result = wf.create_workflow(req.template_name, req.title, req.repo_path or None)
    except wf.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.get("/api/workflows")
async def list_workflows(status: str = Query(None)):
    return db.list_workflow_runs(status=status)


@app.get("/api/workflows/templates")
async def list_templates():
    return {"templates": list(wf.load_templates().values())}


@app.get("/api/workflows/{run_id}")
async def get_workflow(run_id: str):
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    return run


@app.get("/api/workflows/{run_id}/stages")
async def get_workflow_stages(run_id: str):
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    with db.get_db() as conn:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE workflow_run_id=? ORDER BY stage_index",
            (run_id,)).fetchall()
    return [dict(t) for t in tasks]


@app.get("/api/workflows/{run_id}/stage/{stage_key}")
async def get_stage(run_id: str, stage_key: str):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE workflow_run_id=? AND stage_key=?",
            (run_id, stage_key)).fetchone()
    if not row:
        raise HTTPException(status_code=404)
    return dict(row)


@app.post("/api/workflows/{run_id}/approve")
async def approve_stage(run_id: str):
    """批准当前阶段 → 流转下一阶段（代理决策模式下由 agent 调用）"""
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "completed", gate_status="approved")
        db.add_comment(task_id, f"[AUDIT] 阶段流转 action=approve reviewer=user stage={run['current_stage']}",
                       "system")
    wf.advance_stage(run_id)
    return db.get_workflow_run(run_id)


@app.post("/api/workflows/{run_id}/reject")
async def reject_stage(run_id: str, reason: str = Query("")):
    """驳回 → 当前阶段回到 queued（resume 重做）"""
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "queued", gate_status="rejected")
        db.add_comment(task_id, f"[AUDIT] 阶段流转 action=reject stage={run['current_stage']} reason={reason}", "system")
    return db.get_workflow_run(run_id)


@app.post("/api/workflows/{run_id}/force")
async def force_stage(run_id: str, reason: str = Query("")):
    """强制流转（管理员语义，reason 必填）"""
    if not reason:
        raise HTTPException(status_code=400, detail="force 操作 reason 必填")
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "completed", gate_status="forced")
        db.add_comment(task_id, f"[AUDIT] 阶段流转 action=force stage={run['current_stage']} reason={reason}", "system")
    wf.advance_stage(run_id)
    return db.get_workflow_run(run_id)


@app.post("/api/workflows/{run_id}/waive")
async def waive_stage(run_id: str, reason: str = Query("")):
    """豁免当前阶段（跳过流转，reason 必填）"""
    if not reason:
        raise HTTPException(status_code=400, detail="waive 操作 reason 必填")
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "completed", gate_status="waived")
        db.add_comment(task_id, f"[AUDIT] 阶段流转 action=waive stage={run['current_stage']} reason={reason}", "system")
    wf.advance_stage(run_id)
    return db.get_workflow_run(run_id)


def _current_stage_task_id(run_id: str, stage_index: int):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE workflow_run_id=? AND stage_index=?",
            (run_id, stage_index)).fetchone()
    return row["id"] if row else None
```

**注意**: server.py 顶部需 `import workflow as wf`。

**验收**: server 启动后 `curl -s http://localhost:8020/api/workflows/templates` 返回模板列表（T7 后非空）；`curl -s -X POST http://localhost:8020/api/workflows -H 'Content-Type: application/json' -d '{"template_name":"minimal","title":"test"}'` 返回 200 + run dict。

---

## 8. T7: 模板文件

**目标**: `workflows/default.yaml` + `workflows/minimal.yaml`。

**文件**: `workflows/default.yaml`、`workflows/minimal.yaml`（新建目录）

**default.yaml**（五阶段，与设计 §4.2 一致）——完整内容见 `02-design-report.md` §4.2 的代码块（直接复制）。

**minimal.yaml**（二阶段测试模板）：

```yaml
name: minimal
description: 最小二阶段模板（测试用）
version: 1

stages:
  - key: fix
    label: 修复
    index: 0
    agent:
      model: ""
      system_prompt: |
        你是编码工程师。修复指定问题并提交。
    required_artifacts:
      - "*.py"
    gate_rules: "minimal/gate-checks.yaml"
    context_inject:
      prior_stages: []

  - key: verify
    label: 验证
    index: 1
    agent:
      model: ""
      system_prompt: |
        你是验证工程师。检查修复结果，输出验证结论。
    required_artifacts:
      - "*result*.md"
    gate_rules: ""
    context_inject:
      prior_stages: ["fix"]
```

**验收**: `python -c "import workflow; print(sorted(workflow.load_templates()))"` 输出 `['default', 'minimal']`。

---

## 9. T8: 单元测试

**目标**: `tests/test_workflow.py` + `tests/test_gate.py` + `tests/test_qa.py`（unittest-style，与现有测试一致）。

**文件**: `tests/test_workflow.py` 等

**test_workflow.py 核心用例**：

```python
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("PI_ORCHESTRATOR_DB", str(Path(tempfile.mkdtemp()) / "test.db"))

import database as db
import workflow as wf

class TestWorkflow(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_load_templates(self):
        templates = wf.load_templates()
        self.assertIn("default", templates)
        self.assertIn("minimal", templates)

    def test_validate_rejects_bad_index(self):
        bad = {"name": "bad", "stages": [{"key": "a", "index": 1}]}
        with self.assertRaises(wf.WorkflowError):
            wf.validate_template(bad)

    def test_inject_context_has_prior_stages(self):
        stage = {"key": "design", "label": "详细设计",
                 "context_inject": {"prior_stages": ["feasibility"]},
                 "agent": {"system_prompt": "设计吧"}}
        ctx = wf.inject_context(stage, "docs/run1")
        self.assertIn("feasibility", ctx)
        self.assertIn("设计吧", ctx)

    def test_create_workflow_minimal(self):
        result = wf.create_workflow("minimal", "test flow", str(Path(tempfile.mkdtemp())))
        self.assertEqual(result["run"]["current_stage"], "fix")
        self.assertEqual(result["first_task"]["stage_key"], "fix")
```

**test_gate.py 核心用例**：

```python
class TestGate(unittest.TestCase):
    def setUp(self):
        self.artifact = Path(tempfile.mkdtemp())
        (self.artifact / "report.md").write_text(
            "# 报告\n\n## 现状分析\n## 需求分析\n## 技术方案\n## 风险矩阵\n## 结论\n\n```yaml\nname: ok\nstages: []\n```\n",
            encoding="utf-8")

    def test_engine_parses_rules(self):
        rules = self.artifact / "gate-checks.yaml"
        rules.write_text("schema_version: 1\nchecks: []\n", encoding="utf-8")
        engine = gate.GateEngine(rules, self.artifact)
        self.assertEqual(engine.run(), [])

    def test_bad_rules_file_raises(self):
        rules = self.artifact / "bad.yaml"
        rules.write_text("checks: [unclosed", encoding="utf-8")
        with self.assertRaises(gate.GateError):
            gate.GateEngine(rules, self.artifact)

    def test_structure_check(self):
        rules = self.artifact / "gate-checks.yaml"
        rules.write_text(
            "schema_version: 1\nchecks:\n  - id: G1\n    type: structure\n    severity: blocker\n"
            "    target: report.md\n    rule: 章节齐全\n    expected_sections: [现状分析, 需求分析, 技术方案, 风险矩阵, 结论]\n",
            encoding="utf-8")
        engine = gate.GateEngine(rules, self.artifact)
        results = engine.run()
        self.assertTrue(results[0]["passed"])
```

**test_qa.py 核心用例**：

```python
class TestQa(unittest.TestCase):
    def test_custom_rules_detects_print(self):
        d = Path(tempfile.mkdtemp())
        f = d / "a.py"
        f.write_text("print('debug')\n", encoding="utf-8")
        rules = d / "qa-rules.yaml"
        rules.write_text("rules:\n  - id: QA001\n    pattern: 'print\\\\(.*\\\\)'\n    file_glob: '*.py'\n    severity: warning\n    blocking: false\n    description: no print\n",
                         encoding="utf-8")
        findings = qa.run_custom_rules([f], rules)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "QA001")

    def test_generate_report_no_tools(self):
        d = Path(tempfile.mkdtemp())
        md = qa.generate_qa_report([], test_summary={"test_a": {"passed": 1, "failed": 0, "skipped": 0, "duration": 0.1}})
        self.assertIn("最终判定", md)
        self.assertIn("✅ PASS", md)
```

**验收**: `python tests/test_workflow.py && python tests/test_gate.py && python tests/test_qa.py` 全部通过。

---

## 10. T9: 全量回归

**目标**: 现有 49+16 测试 + 新增测试全绿。

**步骤**:
1. `python tests/test_agent.py` → 49 通过
2. `python tests/test_workflow.py` → 通过
3. `python tests/test_gate.py` → 通过
4. `python tests/test_qa.py` → 通过
5. `python tests/test_e2e.py` → 需真实终端（daemon），在 Stage 5 QA 全链路验证

**验收**: 前 4 项全绿；E2E 记录"待 Stage 5 全链路验证"。

---

## 11. 风险与偏差声明

| 项 | 说明 |
|----|------|
| 与原设计偏差 | 无。全部按 02-design-report.md §4-§14 实现 |
| 已知限制 | gate.py 的 `_check_human` 决策写入依赖外部（审批 API 或决策文件），daemon 自动流转在代理决策模式下对 auto_failed 也推进（记录在 gate_result_json） |
| 待验证项 | E2E 全链路（Stage 5）；ruff/mypy 安装后的真实扫描（Stage 4 顺带安装） |

### 11.1 实施阶段的质量门禁（对应设计 §11.3 检查清单）

| 检查点 | 触发时机 | 动作 | 验收 |
|--------|---------|------|------|
| R9 模板预加载 | T2 完成时 | 运行 `python -c "import workflow; workflow.load_templates()"` | default/minimal 均成功加载；构造坏模板应抛 WorkflowError |
| R10 gate 异常 FAIL | T3 完成时 | 用含非法 YAML 的规则文件构造 GateEngine | 抛 GateError（而非静默 PASS） |
| DB 迁移零风险 | T1 完成时 | 对现有 orchestrator.db 运行 `init_db()` | 5 表保留 + 新增 workflow_runs + tasks 5 列，原数据不变 |
| 并发幂等 | T5 完成时 | 审查 advance_stage 乐观锁 UPDATE 语句 | `WHERE id=? AND current_stage_index=?` 条件存在 |

### 11.2 实施顺序的依赖理由

T1（DB）先行是因为所有后续模块（workflow.py 的 create_workflow、daemon 的 gate 状态写入、server 的 workflow 查询）都依赖新表和新列。T2（workflow.py）与 T3（gate.py）无相互依赖，但先做 T2 可以让 T5（daemon 接入）一次性集成两个模块。T7（模板）放在 T5/T6 之前完成，保证 daemon 启动时 `load_templates()` 能加载到真实模板（否则 T5 验收会因空模板而失败）。T8（测试）在核心模块完成后立即补充，用测试锁定行为；T9（回归）最后执行，验证新增代码不破坏现有 49 项单测。

### 11.3 回滚策略

每个任务独立 commit（T1 到 T9 共 9 个 commit），任意任务失败可 `git revert <commit>` 单独回滚而不影响其他任务。workflow 相关代码全部新增或纯增补（数据库层面），回滚 workflow 功能 = 删除新增模块引用 + 保留新增表列（不影响现有单任务路径）。

---

## 12. 下一步

本计划 + gate 三件套构成 Stage 3 交付物。代理决策批准后进入 Stage 4 实施（严格按 T1→T9 顺序）。
