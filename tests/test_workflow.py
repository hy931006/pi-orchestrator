#!/usr/bin/env python3
"""test_workflow.py — workflow 引擎单元测试"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PI_ORCHESTRATOR_DB",
                      str(Path(tempfile.mkdtemp()) / "test-workflow.db"))

import database as db
import workflow as wf

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
    run, task = result["run"], result["first_task"]
    check("创建后 current_stage=fix", run["current_stage"] == "fix", str(run))
    check("首任务 stage_key=fix", task["stage_key"] == "fix", str(task))

    # 5. 阶段流转
    t2 = wf.advance_stage(run["id"])
    check("流转到 verify", t2 is not None and t2["stage_key"] == "verify", str(t2))
    t3 = wf.advance_stage(run["id"])
    check("末阶段后返回 None", t3 is None)
    check("workflow 完成", db.get_workflow_run(run["id"])["status"] == "completed")

    # 6. 并发幂等
    r = wf.create_workflow("minimal", "race", tempfile.mkdtemp())["run"]
    wf.advance_stage(r["id"])
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

    print(f"\n{'='*50}\nWorkflow 测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    test_all()
