"""workflow.py — 工作流编排引擎

职责（对应 02-design-report.md §4/§5）:
- 模板加载（workflows/*.yaml，语法错误 → 拒绝启动，R9 缓解）
- 模板 schema 校验（§4.4）
- 阶段上下文注入（§5.2，文件系统路径引用，不靠 DB）
- workflow 实例创建 + 阶段流转（幂等，乐观锁）
"""
import logging
from pathlib import Path
from typing import Optional

import yaml

import database as db

logger = logging.getLogger("workflow")

WORKFLOWS_DIR = Path(__file__).parent / "workflows"


class WorkflowError(Exception):
    pass


def load_templates() -> dict:
    """扫描 workflows/*.yaml，加载为 {name: template}。语法错误 → 抛 WorkflowError"""
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
    """创建时的 schema 校验（设计 §4.4）"""
    if not template.get("name"):
        raise WorkflowError("模板缺少 name")
    stages = template.get("stages")
    if not stages or not isinstance(stages, list):
        raise WorkflowError("模板 stages 为空或非列表")
    indices = [s.get("index") for s in stages]
    if indices != list(range(len(stages))):
        raise WorkflowError(f"阶段 index 必须连续 0..{len(stages) - 1}: {indices}")
    for s in stages:
        if not s.get("required_artifacts"):
            raise WorkflowError(f"阶段 {s.get('key')} 缺少 required_artifacts")
        if not s.get("agent", {}).get("system_prompt"):
            raise WorkflowError(f"阶段 {s.get('key')} 缺少 agent.system_prompt")


def inject_context(stage: dict, artifact_dir: str) -> str:
    """构造包含上游产物的 prompt 前缀（设计 §5.2）

    上下文传递不靠 DB、不靠全局变量——仅靠文件系统路径引用。
    pi 子任务通过工具调用 read_file 自主读取上游产物。
    """
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
            lines.append("  请读取该目录下的报告文件，理解其结论和约束")
        lines.append("")
    lines.append("## 本阶段任务")
    lines.append(stage["agent"].get("system_prompt", ""))
    return "\n".join(lines)


def _bind_stage_task(task: dict, run_id: str, stage: dict) -> None:
    """把 task 绑定到 workflow 阶段（写 workflow_run_id/stage_key/stage_index）"""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET workflow_run_id=?, stage_key=?, stage_index=?, "
            "gate_status='pending' WHERE id=?",
            (run_id, stage["key"], stage["index"], task["id"]))


def create_workflow(template_name: str, title: str, repo_path: str = None) -> dict:
    """创建 workflow 实例 + 第一个阶段任务（设计 §9.2 POST /api/workflows）"""
    templates = load_templates()
    if template_name not in templates:
        raise WorkflowError(f"未知模板: {template_name}，可用: {sorted(templates)}")
    template = templates[template_name]
    validate_template(template)

    run = db.create_workflow_run(template_name, title, repo_path)
    stages = template["stages"]
    first = stages[0]

    task = db.create_task(
        title=f"[{run['id']}] {first['label']}",
        description=inject_context(first, run["artifact_dir"]),
        agent_type="auto",
        repo_path=run["repo_path"],
    )
    _bind_stage_task(task, run["id"], first)
    db.update_workflow_run(run["id"], current_stage=first["key"], current_stage_index=first["index"])
    run = db.get_workflow_run(run["id"])  # 重读，返回更新后的实例
    task = db.get_task(task["id"])        # 重读，返回带 stage_key 的实例

    logger.info(f"🆕 workflow {run['id']} 创建: {template_name} → stage {first['key']}")
    return {"run": run, "first_task": task, "template": template}


def advance_stage(run_id: str) -> Optional[dict]:
    """当前阶段完成后推进到下一阶段（幂等，乐观锁）。

    返回新阶段的 task；全部完成返回 None。
    并发竞争（乐观锁冲突）返回 None（丢弃本次推进）。
    """
    run = db.get_workflow_run(run_id)
    if not run:
        raise WorkflowError(f"workflow 不存在: {run_id}")
    templates = load_templates()
    template = templates.get(run["template_name"])
    if not template:
        raise WorkflowError(f"模板 {run['template_name']} 丢失")
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
            "UPDATE tasks SET workflow_run_id=?, stage_key=?, stage_index=?, "
            "gate_status='pending' WHERE id=?",
            (run_id, stage["key"], next_idx, task["id"]))
    logger.info(f"➡️ workflow {run_id} → stage {stage['key']}")
    return db.get_task(task["id"])  # 重读，返回带 stage_key 的实例
