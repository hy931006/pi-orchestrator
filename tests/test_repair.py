#!/usr/bin/env python3
"""test_repair.py — gate 失败 → repair 分支（R1 条件路由）测试

覆盖:
- 模板校验（on_gate_fail / max_repairs / repair 嵌套禁止）
- repair 节点不作为入口、不参与正常 DAG 解锁、不计入完成条件
- gate 失败阻断主线流转（不再无条件放行）
- route_gate_failure 创建/重开/幂等/上限
- handle_repair_result 三分支（unlocked/retry/exhausted）
- daemon._after_stage 端到端（真实 gate.yaml + 产物目录，echo backend）
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PI_ORCHESTRATOR_DB",
                      str(Path(tempfile.mkdtemp()) / "test-repair.db"))

import database as db  # noqa: E402
import workflow as wf  # noqa: E402
import daemon as daemon_mod  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ── 测试模板（独立 WORKFLOWS_DIR，不污染项目模板）──
TMPL_DIR = Path(tempfile.mkdtemp())
(TMPL_DIR / "repairflow.yaml").write_text("""\
name: repairflow
description: gate-fail → repair 测试模板
version: 1
stages:
  - key: build
    label: 构建
    index: 0
    agent: {system_prompt: "构建"}
    required_artifacts: ["*.md"]
    gate_rules: gate.yaml
    on_gate_fail: fix_build
    max_repairs: 2
    depends_on: []
  - key: fix_build
    label: 修复构建
    index: 1
    type: repair
    agent: {system_prompt: "修复"}
    required_artifacts: ["*.md"]
    depends_on: []
  - key: ship
    label: 发布
    index: 2
    agent: {system_prompt: "发布"}
    required_artifacts: ["*.md"]
    depends_on: [build]
""", encoding="utf-8")
(TMPL_DIR / "plain.yaml").write_text("""\
name: plain
description: 无 repair 路由模板
version: 1
stages:
  - key: only
    label: 唯一阶段
    index: 0
    agent: {system_prompt: "做"}
    required_artifacts: ["*.md"]
    depends_on: []
""", encoding="utf-8")
(TMPL_DIR / "sharedrepair.yaml").write_text("""\
name: sharedrepair
description: 共享 repair 节点（多父阶段）测试模板
version: 1
stages:
  - key: a
    label: 阶段A
    index: 0
    agent: {system_prompt: "做A"}
    required_artifacts: ["*.md"]
    on_gate_fail: fix
    depends_on: []
  - key: b
    label: 阶段B
    index: 1
    agent: {system_prompt: "做B"}
    required_artifacts: ["*.md"]
    on_gate_fail: fix
    depends_on: [a]
  - key: fix
    label: 通用修复
    index: 90
    type: repair
    agent: {system_prompt: "修"}
    required_artifacts: ["*"]
    depends_on: []
""", encoding="utf-8")
wf.WORKFLOWS_DIR = TMPL_DIR


def fresh_run(template="repairflow"):
    return wf.create_workflow(template, "t", tempfile.mkdtemp())


def set_task(task_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.get_db() as conn:
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?",
                     (*fields.values(), task_id))


def get_stage_task(run_id, stage_key):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE workflow_run_id=? AND stage_key=? "
            "ORDER BY created_at DESC LIMIT 1", (run_id, stage_key)).fetchone()
    return dict(row) if row else None


def test_all():
    db.init_db()
    templates = wf.load_templates()
    tmpl = templates["repairflow"]

    # ── 1. 模板校验 ──
    wf.validate_template(tmpl)
    check("合法 repair 模板通过校验", True)

    def expect_reject(name, mutate):
        import copy
        bad = copy.deepcopy(tmpl)
        mutate(bad)
        try:
            wf.validate_template(bad)
            check(name, False, "未被拒绝")
        except wf.WorkflowError:
            check(name, True)

    expect_reject("on_gate_fail 指向未定义阶段 → 拒绝",
                  lambda t: t["stages"][0].update(on_gate_fail="ghost"))
    expect_reject("on_gate_fail 指向非 repair 阶段 → 拒绝",
                  lambda t: t["stages"][0].update(on_gate_fail="ship"))
    expect_reject("repair 节点嵌套 on_gate_fail → 拒绝",
                  lambda t: t["stages"][1].update(on_gate_fail="ship"))
    expect_reject("max_repairs=0 → 拒绝",
                  lambda t: t["stages"][0].update(max_repairs=0))

    # ── 2. repair 节点不作为入口 ──
    res = fresh_run()
    run = res["run"]
    check("入口仅 build（repair 不作入口）",
          [t["stage_key"] for t in res["first_tasks"]] == ["build"],
          str([t["stage_key"] for t in res["first_tasks"]]))
    check("fix_build 未在创建时生成", get_stage_task(run["id"], "fix_build") is None)

    # ── 3. gate 失败阻断主线（不再无条件放行）──
    build = res["first_tasks"][0]
    set_task(build["id"], status="completed", gate_status="auto_failed")
    unlocked = wf.unlock_next_stages(run["id"])
    check("gate auto_failed → ship 不解锁", unlocked == [], str(unlocked))
    check("ship 任务未创建", get_stage_task(run["id"], "ship") is None)

    # ── 4. route_gate_failure 创建 repair 任务 ──
    rt = wf.route_gate_failure(run, db.get_task(build["id"]), tmpl)
    check("路由创建 fix_build 任务", rt is not None and rt["stage_key"] == "fix_build")
    check("repair 任务 queued", rt["status"] == "queued")
    check("repair 任务标题带 🔧", rt["title"].startswith("🔧"), rt["title"])
    check("repair 上下文注入 gate-result 路径",
          "gate-result.md" in rt["description"] and "build" in rt["description"],
          rt["description"][:120])
    # 幂等：queued 状态再路由 → 返回同一个任务，不重复创建
    rt2 = wf.route_gate_failure(run, db.get_task(build["id"]), tmpl)
    check("queued 中重复路由 → 幂等返回同任务", rt2["id"] == rt["id"])

    # ── 5. repair 通过 → 父阶段 repaired → 下游解锁 ──
    set_task(rt["id"], status="completed", gate_status="auto_passed")
    action = wf.handle_repair_result(run, db.get_task(rt["id"]), tmpl, passed=True)
    check("repair 通过 → unlocked", action == "unlocked", action)
    check("父阶段 gate_status=repaired",
          get_stage_task(run["id"], "build")["gate_status"] == "repaired")
    unlocked = wf.unlock_next_stages(run["id"])
    check("repaired 后 ship 解锁",
          [t["stage_key"] for t in unlocked] == ["ship"], str(unlocked))

    # ship 完成 → 整条 workflow 完成（repair 节点不计入完成条件）
    ship = get_stage_task(run["id"], "ship")
    set_task(ship["id"], status="completed", gate_status="auto_passed")
    wf.unlock_next_stages(run["id"])
    check("主线全完成 → workflow completed（repair 不计数）",
          db.get_workflow_run(run["id"])["status"] == "completed")

    # ── 6. repair 自旋：retry → retry → exhausted ──
    res2 = fresh_run()
    run2 = res2["run"]
    b2 = res2["first_tasks"][0]
    set_task(b2["id"], status="completed", gate_status="auto_failed")
    rt = wf.route_gate_failure(run2, db.get_task(b2["id"]), tmpl)
    # 第 1 次修复失败
    set_task(rt["id"], status="completed", gate_status="auto_failed")
    a1 = wf.handle_repair_result(run2, db.get_task(rt["id"]), tmpl, passed=False)
    t1 = db.get_task(rt["id"])
    check("第 1 次失败 → retry", a1 == "retry", a1)
    check("repair_count=1 且回 queued",
          t1["repair_count"] == 1 and t1["status"] == "queued", str(t1))
    # 第 2 次修复失败（达上限 max_repairs=2）
    set_task(rt["id"], status="completed", gate_status="auto_failed")
    a2 = wf.handle_repair_result(run2, db.get_task(rt["id"]), tmpl, passed=False)
    t2 = db.get_task(rt["id"])
    check("第 2 次失败 → exhausted（达上限）", a2 == "exhausted", a2)
    check("exhausted 后不再重开", t2["status"] == "completed" and t2["repair_count"] == 2)
    # 上限后再路由 → None（等人工）
    rt_again = wf.route_gate_failure(run2, db.get_task(b2["id"]), tmpl)
    check("达上限后 route 返回 None（等人工）", rt_again is None)
    # ship 始终未解锁
    check("自旋期间 ship 始终未解锁", get_stage_task(run2["id"], "ship") is None)

    # ── 7. 无 on_gate_fail 的模板：route 返回 None ──
    res3 = fresh_run("plain")
    only = res3["first_tasks"][0]
    set_task(only["id"], status="completed", gate_status="auto_failed")
    rt3 = wf.route_gate_failure(res3["run"], db.get_task(only["id"]),
                                templates["plain"])
    check("无 on_gate_fail → route None（停住等人工）", rt3 is None)

    # ── 8. 共享 repair 节点：多父阶段归属（repair_for 区分）──
    stmpl = templates["sharedrepair"]
    sres = fresh_run("sharedrepair")
    srun = sres["run"]
    sa = sres["first_tasks"][0]
    set_task(sa["id"], status="completed", gate_status="auto_failed")
    sfix_a = wf.route_gate_failure(srun, db.get_task(sa["id"]), stmpl)
    check("共享节点: A 失败 → repair_for=a",
          sfix_a is not None and sfix_a.get("repair_for") == "a", str(sfix_a))
    set_task(sfix_a["id"], status="completed", gate_status="auto_passed")
    wf.handle_repair_result(srun, db.get_task(sfix_a["id"]), stmpl, passed=True)
    check("共享节点: A 被标记 repaired",
          get_stage_task(srun["id"], "a")["gate_status"] == "repaired")
    unlocked = wf.unlock_next_stages(srun["id"])
    check("共享节点: A repaired → b 解锁",
          [t["stage_key"] for t in unlocked] == ["b"], str(unlocked))
    # b 失败 → 应创建「新的」fix 任务（不与 A 的复用），repair_for=b
    sb = get_stage_task(srun["id"], "b")
    set_task(sb["id"], status="completed", gate_status="auto_failed")
    sfix_b = wf.route_gate_failure(srun, db.get_task(sb["id"]), stmpl)
    check("共享节点: b 失败 → 新建 fix 任务（不复用 A 的）",
          sfix_b is not None and sfix_b["id"] != sfix_a["id"])
    check("共享节点: b 的 repair_for=b", sfix_b.get("repair_for") == "b")
    set_task(sfix_b["id"], status="completed", gate_status="auto_passed")
    wf.handle_repair_result(srun, db.get_task(sfix_b["id"]), stmpl, passed=True)
    check("共享节点: b 被标记 repaired（不会误标 a 以外的）",
          get_stage_task(srun["id"], "b")["gate_status"] == "repaired")
    wf.unlock_next_stages(srun["id"])
    check("共享节点: 全程结束 workflow completed",
          db.get_workflow_run(srun["id"])["status"] == "completed")

    # ── 9. daemon 级端到端：真实 gate.yaml + 产物目录 ──
    cwd = os.getcwd()
    tmpcwd = Path(tempfile.mkdtemp())
    os.chdir(tmpcwd)
    try:
        res4 = fresh_run()
        run4 = res4["run"]
        b4 = res4["first_tasks"][0]
        artifact = Path(run4["artifact_dir"])          # docs/<run_id>（相对 cwd）
        (artifact / "build").mkdir(parents=True)
        # 失败规则：要求 build/ 下存在非空 .md（此时没有 → FAIL）
        (artifact / "gate.yaml").write_text("""\
checks:
  - id: G1
    type: structure
    severity: blocker
    target: "*.md"
    rule: 必须产出非空 markdown 报告
""", encoding="utf-8")
        d = daemon_mod.TaskExecutor("echo")
        # 主线节点完成（gate 必失败）→ 应路由 repair 而非解锁 ship
        d._after_stage(db.get_task(b4["id"]), None)
        b4_after = db.get_task(b4["id"])
        check("daemon: gate 失败标记 auto_failed",
              b4_after["gate_status"] == "auto_failed", b4_after["gate_status"])
        fix4 = get_stage_task(run4["id"], "fix_build")
        check("daemon: 自动创建 repair 任务", fix4 is not None and fix4["status"] == "queued")
        check("daemon: ship 未解锁", get_stage_task(run4["id"], "ship") is None)
        # repair 执行后产出修复产物 → gate 应通过
        (artifact / "build" / "report.md").write_text("# 修复完成\n", encoding="utf-8")
        set_task(fix4["id"], status="running")  # 模拟被 daemon 领取执行
        d._after_stage(db.get_task(fix4["id"]), None)
        check("daemon: repair 后父阶段 repaired",
              get_stage_task(run4["id"], "build")["gate_status"] == "repaired")
        check("daemon: repair 通过 → ship 解锁",
              get_stage_task(run4["id"], "ship") is not None)
    finally:
        os.chdir(cwd)


if __name__ == "__main__":
    test_all()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
