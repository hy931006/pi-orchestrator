"""workflow.py — 工作流编排引擎

职责（对应 02-design-report.md §4/§5）:
- 模板加载（workflows/*.yaml，语法错误 → 拒绝启动，R9 缓解）
- 模板 schema 校验（§4.4）
- 阶段上下文注入（§5.2，文件系统路径引用，不靠 DB）
- workflow 实例创建 + 阶段流转（幂等，乐观锁）
"""
import logging
import threading
from pathlib import Path
from typing import Optional

import yaml

import database as db

logger = logging.getLogger("workflow")

WORKFLOWS_DIR = Path(__file__).parent / "workflows"

# 保护 unlock_next_stages 的检查-创建区间（SQLite 无行级锁，多线程竞态防护）
_UNLOCK_LOCK = threading.Lock()


class WorkflowError(Exception):
    pass


def load_templates() -> dict:
    """扫描 workflows/*.yaml，加载为 {name: template}。语法错误 → 抛 WorkflowError

    自动补全: 若阶段缺少 depends_on，按 index 线性推导（兼容旧模板）。
    """
    templates = {}
    if not WORKFLOWS_DIR.exists():
        return templates
    for f in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            stages = data.get("stages") or []
            # 按 index 排序后补 depends_on（无显式依赖 → 依赖前一个阶段）
            stages_sorted = sorted(stages, key=lambda s: s.get("index", 0))
            for i, s in enumerate(stages_sorted):
                if "depends_on" not in s:
                    s["depends_on"] = [stages_sorted[i - 1]["key"]] if i > 0 else []
            data["stages"] = stages_sorted
            templates[data["name"]] = data
        except (yaml.YAMLError, KeyError) as e:
            raise WorkflowError(f"模板 {f.name} 解析失败: {e}")
    return templates


def validate_template(template: dict) -> None:
    """创建时的 schema 校验（设计 §4.4 + DAG 扩展）"""
    if not template.get("name"):
        raise WorkflowError("模板缺少 name")
    stages = template.get("stages")
    if not stages or not isinstance(stages, list):
        raise WorkflowError("模板 stages 为空或非列表")
    keys = [s.get("key") for s in stages]
    if len(set(keys)) != len(keys):
        raise WorkflowError(f"阶段 key 必须唯一: {keys}")
    for s in stages:
        if not s.get("required_artifacts"):
            raise WorkflowError(f"阶段 {s.get('key')} 缺少 required_artifacts")
        # ui_design 节点：用内置 impeccable 指令，无需 agent 绑定
        if s.get("type") == "ui_design":
            if not s.get("ui_target"):
                raise WorkflowError(f"UI 设计节点 {s.get('key')} 缺少 ui_target（目标文件/目录）")
        else:
            # agent 绑定：agent_ref 引用 agents 表（持久实体），或内嵌 system_prompt
            agent_ref = s.get("agent_ref")
            if agent_ref:
                if not db.get_agent_by_name(agent_ref):
                    raise WorkflowError(
                        f"阶段 {s.get('key')} 引用的 agent '{agent_ref}' 不存在（先在 Agent 管理中创建）")
            else:
                if not s.get("agent", {}).get("system_prompt"):
                    raise WorkflowError(f"阶段 {s.get('key')} 缺少 agent.system_prompt 或 agent_ref")
        # DAG 依赖校验：depends_on 必须指向已定义阶段，且不能自依赖
        deps = s.get("depends_on") or []
        for d in deps:
            if d not in keys:
                raise WorkflowError(f"阶段 {s.get('key')} 依赖未定义的阶段 {d}")
            if d == s.get("key"):
                raise WorkflowError(f"阶段 {s.get('key')} 不能依赖自身")
    # 有向无环图校验（DFS 检测环）
    _check_dag_cycle(stages)


def _check_dag_cycle(stages: list) -> None:
    """DFS 检测 DAG 环"""
    visiting, visited = set(), set()

    def dfs(key: str):
        if key in visiting:
            raise WorkflowError(f"DAG 存在环: {key}")
        if key in visited:
            return
        visiting.add(key)
        for s in stages:
            if key in (s.get("depends_on") or []):
                dfs(s["key"])
        visiting.discard(key)
        visited.add(key)

    for s in stages:
        dfs(s["key"])


def _entry_stages(template: dict) -> list:
    """无依赖的阶段 = 入口节点（首批执行）"""
    stages = template["stages"]
    return [s for s in stages if not (s.get("depends_on") or [])]


def _next_stages(template: dict, done_keys: set) -> list:
    """依赖全部完成且自身未完成的阶段 = 可解锁节点"""
    stages = template["stages"]
    ready = []
    for s in stages:
        if s["key"] in done_keys:
            continue
        deps = s.get("depends_on") or []
        if all(d in done_keys for d in deps):
            ready.append(s)
    return ready


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
    if stage.get("type") == "ui_design":
        # UI 设计节点：注入 impeccable 设计指令（.pi/skills/impeccable/）
        ui_target = stage.get("ui_target") or "."
        lines.append(_ui_design_instruction(ui_target))
    else:
        agent_ref = stage.get("agent_ref")
        if agent_ref:
            # agent 是持久实体：daemon 从 agents 表加载 system_prompt + model
            lines.append(f"（本阶段绑定自定义 Agent: {agent_ref}，由 daemon 注入其人格与模型）")
        else:
            lines.append(stage["agent"].get("system_prompt", ""))
    return "\n".join(lines)


def _ui_design_instruction(target: str) -> str:
    """UI 设计节点指令：impeccable 设计系统（Operate 模式，工具类 UI）

    要求 pi 子进程调用本项目的 impeccable skill 完成设计与自检。
    """
    return f"""你是 award-winning 的设计总监。使用本项目已安装的 impeccable 设计技能
（.pi/skills/impeccable/）对目标 UI 做专业设计/优化。

## 目标
- UI 目标: {target}
- 模式: Operate（工具类应用 UI——可扫描性、一致性、专业感优先）

## 执行步骤
1. 运行 `node .pi/skills/impeccable/scripts/context.mjs --target {target}` 加载设计上下文
2. 阅读 .pi/skills/impeccable/SKILL.md 的 Commands 表和 anti-patterns
3. 对目标执行 impeccable audit（参考 reference/audit.md）
4. 修复发现的问题，重点检查:
   - 低对比度文字（WCAG AA ≥4.5:1）
   - 过小字号（body <14px）
   - AI 签名特征: 像素网格背景/紫蓝渐变/卡片套卡片/Inter 字体/纯黑纯灰
   - 间距节奏与对齐一致性
5. 用 `npx impeccable detect {target}` 验证: 反模式数应为 0

## 纪律
- 保持现有功能与 JS 依赖的 ID/class 不变，只改样式与结构微调
- 完成后报告: 修复了哪些问题、detect 前后对比
"""


def _bind_stage_task(task: dict, run_id: str, stage: dict) -> None:
    """把 task 绑定到 workflow 阶段（写 workflow_run_id/stage_key/stage_index）"""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET workflow_run_id=?, stage_key=?, stage_index=?, "
            "gate_status='pending' WHERE id=?",
            (run_id, stage["key"], stage["index"], task["id"]))


def create_workflow(template_name: str, title: str, repo_path: str = None) -> dict:
    """创建 workflow 实例 + 首批入口阶段任务（设计 §9.2 POST /api/workflows）"""
    templates = load_templates()
    if template_name not in templates:
        raise WorkflowError(f"未知模板: {template_name}，可用: {sorted(templates)}")
    template = templates[template_name]
    validate_template(template)

    run = db.create_workflow_run(template_name, title, repo_path)
    entries = _entry_stages(template)
    if not entries:
        db.update_workflow_run(run["id"], status="completed")
        raise WorkflowError("模板无入口阶段（所有阶段都有依赖且构成环）")

    tasks = []
    for stage in entries:
        task = _create_stage_task(run, stage)
        tasks.append(task)
    db.update_workflow_run(run["id"], current_stage=entries[0]["key"],
                           current_stage_index=min(s.get("index", 0) for s in entries))

    logger.info(f"🆕 workflow {run['id']} 创建: {template_name} → 入口 {[s['key'] for s in entries]}")
    return {"run": db.get_workflow_run(run["id"]), "first_tasks": tasks, "template": template}


def _create_stage_task(run: dict, stage: dict) -> dict:
    """为单个阶段创建 task 并绑定 workflow 字段

    agent 绑定策略（agent 是持久实体，不内嵌 prompt）:
      - stage.agent_ref = agents 表 name → task.agent_type = agent id
        （daemon 执行时从 agents 表加载 system_prompt + model）
      - stage.agent.system_prompt 内嵌 → 不设 agent_type（用内嵌 prompt）
    """
    agent_ref = stage.get("agent_ref")
    agent_type = "auto"
    if agent_ref:
        agent = db.get_agent_by_name(agent_ref)
        if agent:
            agent_type = agent["id"]
    task = db.create_task(
        title=f"[{run['id']}] {stage.get('label', stage['key'])}",
        description=inject_context(stage, run["artifact_dir"]),
        agent_type=agent_type,
        repo_path=run["repo_path"],
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET workflow_run_id=?, stage_key=?, stage_index=?, "
            "gate_status='pending', model=? WHERE id=?",
            (run["id"], stage["key"], stage.get("index", 0),
             stage.get("agent", {}).get("model", ""), task["id"]))
    return db.get_task(task["id"])


def unlock_next_stages(run_id: str) -> list[dict]:
    """当前节点完成后，按依赖解锁下一批可运行节点（幂等，乐观锁）。

    返回新创建的任务列表；全部完成返回 []。支持 DAG 并行分支：
    多个依赖已满足的节点同时解锁。
    """
    run = db.get_workflow_run(run_id)
    if not run:
        raise WorkflowError(f"workflow 不存在: {run_id}")
    templates = load_templates()
    template = templates.get(run["template_name"])
    if not template:
        raise WorkflowError(f"模板 {run['template_name']} 丢失")

    # 已完成节点 = tasks 表中 status=completed 的 stage_key 集合
    with _UNLOCK_LOCK:  # 串行化检查-创建，防并发重复解锁
        with db.get_db() as conn:
            done = {r["stage_key"] for r in conn.execute(
                "SELECT stage_key FROM tasks WHERE workflow_run_id=? AND status IN "
                "('completed','waived','forced','approved')", (run_id,)).fetchall()}
            all_stage_keys = {r["stage_key"] for r in conn.execute(
                "SELECT stage_key FROM tasks WHERE workflow_run_id=?", (run_id,)).fetchall()}

        # 全部阶段已完成？
        total = len(template["stages"])
        if len(done) >= total:
            db.update_workflow_run(run_id, status="completed")
            logger.info(f"✅ workflow {run_id} 全部阶段完成")
            return []

        # 可解锁节点 = 依赖全完成且尚未创建
        new_tasks = []
        for stage in _next_stages(template, done):
            if stage["key"] in all_stage_keys:
                continue  # 已创建，跳过（幂等）
            task = _create_stage_task(run, stage)
            new_tasks.append(task)
            logger.info(f"➡️ workflow {run_id} 解锁 stage {stage['key']}")

    if new_tasks:
        db.update_workflow_run(run_id, current_stage=new_tasks[0]["stage_key"])
    else:
        # 有已完成节点但无新解锁 → 检查是否因失败阻塞（留在当前状态等待人工）
        logger.info(f"⏸️ workflow {run_id} 无新解锁节点（可能等待人工干预）")
    return new_tasks


# 兼容旧接口：advance_stage = unlock_next_stages 的线性特例（返回第一个新任务或 None）
def advance_stage(run_id: str) -> Optional[dict]:
    """旧版线性推进接口（兼容）。返回第一个新任务；无新任务返回 None。"""
    tasks = unlock_next_stages(run_id)
    return tasks[0] if tasks else None
