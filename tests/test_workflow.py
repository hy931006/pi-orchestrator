#!/usr/bin/env python3
"""test_workflow.py — workflow 引擎单元测试"""
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PI_ORCHESTRATOR_DB",
                      str(Path(tempfile.mkdtemp()) / "test-workflow.db"))

import database as db  # noqa: E402
import workflow as wf  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def test_all():
    db.init_db()

    # 1. 模板加载
    templates = wf.load_templates()
    check("模板加载 default+minimal",
          sorted(templates) == ["default", "minimal"], str(sorted(templates)))

    # 2. 校验拒绝坏模板
    try:
        wf.validate_template({"name": "bad", "stages": [{"key": "a", "index": 1}]})
        check("坏模板被拒绝", False)
    except wf.WorkflowError:
        check("坏模板被拒绝", True)

    # 3. 上下文注入
    stage = {"key": "design", "label": "详细设计",
             "context_inject": {"prior_stages": ["feasibility"]},
             "agent": {"system_prompt": "设计吧"}}
    ctx = wf.inject_context(stage, "docs/run1")
    check("上下文注入含上游+任务", "feasibility" in ctx and "设计吧" in ctx)

    # 4. 创建 workflow
    result = wf.create_workflow("minimal", "test flow", tempfile.mkdtemp())
    run = result["run"]
    task = result["first_tasks"][0]
    check("创建后 current_stage=fix", run["current_stage"] == "fix", str(run))
    check("首任务 stage_key=fix", task["stage_key"] == "fix", str(task))

    # 5. 阶段流转（DAG 解锁：fix 完成后 → verify）
    with db.get_db() as conn:
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
    t2 = wf.advance_stage(run["id"])
    check("流转到 verify", t2 is not None and t2["stage_key"] == "verify", str(t2))
    with db.get_db() as conn:
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (t2["id"],))
    t3 = wf.advance_stage(run["id"])
    check("末阶段后返回 None", t3 is None)
    check("workflow 完成", db.get_workflow_run(run["id"])["status"] == "completed")

    # 6. 并发幂等
    r = wf.create_workflow("minimal", "race", tempfile.mkdtemp())["run"]
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE workflow_run_id=? AND stage_key='fix'",
            (r["id"],)).fetchone()
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (row["id"],))
    results = []

    def adv():
        results.append(wf.advance_stage(r["id"]))

    threads = [threading.Thread(target=adv) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    non_none = [x for x in results if x is not None]
    check("并发推进 ≤1 次生效", len(non_none) <= 1, f"{len(non_none)}/2")

    # 7. 未知模板报错
    try:
        wf.create_workflow("nope", "x")
        check("未知模板报错", False)
    except wf.WorkflowError:
        check("未知模板报错", True)

    # 8. DAG 并行分支：A → (B,C 并行) → D
    dag = {
        "name": "dag-test",
        "stages": [
            {"key": "A", "label": "A", "index": 0, "depends_on": [],
             "agent": {"system_prompt": "A"}, "required_artifacts": ["a.md"]},
            {"key": "B", "label": "B", "index": 1, "depends_on": ["A"],
             "agent": {"system_prompt": "B"}, "required_artifacts": ["b.md"]},
            {"key": "C", "label": "C", "index": 2, "depends_on": ["A"],
             "agent": {"system_prompt": "C"}, "required_artifacts": ["c.md"]},
            {"key": "D", "label": "D", "index": 3, "depends_on": ["B", "C"],
             "agent": {"system_prompt": "D"}, "required_artifacts": ["d.md"]},
        ],
    }
    import workflow as wf_mod
    from pathlib import Path as _P
    wf_dir = _P(__file__).parent.parent / "workflows"
    dag_file = wf_dir / "dag-test.yaml"
    dag_file.write_text(_yaml_dump(dag), encoding="utf-8")
    try:
        wf_mod.validate_template(dag)
        # 直接调用内部逻辑：用内存模板验证解锁
        run = db.create_workflow_run("dag-test", "dag", tempfile.mkdtemp())
        a_task = wf_mod._create_stage_task(run, dag["stages"][0])
        with db.get_db() as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (a_task["id"],))
            conn.execute("UPDATE workflow_runs SET current_stage='A', current_stage_index=0 WHERE id=?",
                         (run["id"],))
        # A 完成后 → B、C 同时解锁
        unlocked = wf_mod.unlock_next_stages(run["id"])
        keys = sorted(t["stage_key"] for t in unlocked)
        check("A 完成后并行解锁 B+C", keys == ["B", "C"], str(keys))
        # B 完成 → D 还不能解锁（C 未完成）
        b_id = next(t["id"] for t in unlocked if t["stage_key"] == "B")
        with db.get_db() as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (b_id,))
        unlocked2 = wf_mod.unlock_next_stages(run["id"])
        check("B 完成时 D 不提前解锁（等 C）", unlocked2 == [], str([t["stage_key"] for t in unlocked2]))
        # C 完成 → D 解锁
        c_id = next(t["id"] for t in unlocked if t["stage_key"] == "C")
        with db.get_db() as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (c_id,))
        unlocked3 = wf_mod.unlock_next_stages(run["id"])
        check("B+C 完成后 D 解锁", [t["stage_key"] for t in unlocked3] == ["D"],
              str([t["stage_key"] for t in unlocked3]))
        # D 完成 → workflow completed
        d_id = unlocked3[0]["id"]
        with db.get_db() as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (d_id,))
        wf_mod.unlock_next_stages(run["id"])
        check("全部完成后 workflow completed",
              db.get_workflow_run(run["id"])["status"] == "completed")
    finally:
        dag_file.unlink(missing_ok=True)

    # 9. DAG 环检测（纯内存校验，无需模板文件）
    cyc = {"name": "cyc", "stages": [
        {"key": "X", "depends_on": ["Y"], "agent": {"system_prompt": "x"},
         "required_artifacts": ["x.md"]},
        {"key": "Y", "depends_on": ["X"], "agent": {"system_prompt": "y"},
         "required_artifacts": ["y.md"]},
    ]}
    try:
        wf_mod.validate_template(cyc)
        check("DAG 环被拒绝", False)
    except wf.WorkflowError:
        check("DAG 环被拒绝", True)

    print(f"\n{'='*50}\nWorkflow 测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


def _yaml_dump(d):
    import yaml
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    test_all()
