#!/usr/bin/env python3
"""test_workflow_e2e.py — workflow 编排端到端测试（server + daemon + mock pi + minimal 模板）

验证:
  W1 创建 workflow → fix 阶段任务进入队列
  W2 daemon 执行 fix 阶段（mock pi 完成）→ gate 检查 → 自动流转 verify
  W3 verify 阶段完成后 → workflow completed
  W4 单任务入口仍可用（Q9 共存）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
PORT = 8141
BASE = f"http://127.0.0.1:{PORT}"
MOCK_PI = Path(__file__).parent / "mock_pi.py"

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def wait_status(run_id, want, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = api("GET", f"/api/workflows/{run_id}")
        if r["status"] in want:
            return r
        time.sleep(1)
    return None


def make_shim(dir_):
    """构造 pi.cmd + pi.ps1 shim（Windows #3306 路径）"""
    if sys.platform == "win32":
        ps1 = dir_ / "pi.ps1"
        cmd = dir_ / "pi.cmd"
        ps1.write_text(f'& "{sys.executable}" "$PSScriptRoot\\mock_pi.py" @args\nexit $LASTEXITCODE\n',
                       encoding="utf-8")
        cmd.write_text(f'@echo off\n"{sys.executable}" "%~dp0mock_pi.py" %*\n', encoding="utf-8")
    else:
        sh = dir_ / "pi"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{dir_}/mock_pi.py" "$@"\n')
        sh.chmod(0o755)
    shutil.copy(MOCK_PI, dir_ / "mock_pi.py")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wf-e2e-"))
    shim_dir = tmp / "bin"
    shim_dir.mkdir()
    make_shim(shim_dir)

    # 测试用 git 仓库（workflow 产物提交目标）
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo])
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t.t"])
    subprocess.run(["git", "-C", repo, "config", "user.name", "test"])

    env = os.environ.copy()
    env["PI_ORCHESTRATOR_DB"] = str(tmp / "test.db")
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["PI_MOCK_ARGV_FILE"] = str(tmp / "argv.jsonl")

    server_log = open(tmp / "server.log", "w", encoding="utf-8")
    daemon_log = open(tmp / "daemon.log", "w", encoding="utf-8")

    server = subprocess.Popen([sys.executable, "run.py", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
    daemon = None
    try:
        for _ in range(30):
            try:
                api("GET", "/health")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("server 未能启动")

        daemon = subprocess.Popen([sys.executable, "daemon.py"], cwd=ROOT, env=env,
                                  stdout=daemon_log, stderr=subprocess.STDOUT)
        time.sleep(2)

        # ── W1 创建 workflow ──
        print("\n[W1] 创建 workflow (minimal)")
        w = api("POST", "/api/workflows",
                {"template_name": "minimal", "title": "E2E flow", "repo_path": str(repo)})
        run_id = w["run"]["id"]
        check("workflow 创建", run_id and w["run"]["current_stage"] == "fix", str(w))
        stages = api("GET", f"/api/workflows/{run_id}/stages")["stages"]
        check("首阶段任务 queued", len(stages) == 1 and stages[0]["status"] == "queued",
              str([s["status"] for s in stages]))
        task_id = stages[0]["id"]

        # ── W2 daemon 执行 fix → gate → 流转 verify ──
        print("\n[W2] fix 阶段执行 + 流转")
        done = None
        deadline = time.time() + 90
        while time.time() < deadline:
            stages = api("GET", f"/api/workflows/{run_id}/stages")["stages"]
            if len(stages) >= 2:
                done = True
                break
            time.sleep(1)
        check("自动流转到 verify（阶段任务数 2）", done, str(len(stages)))
        if done:
            fix_task = api("GET", f"/api/tasks/{task_id}")
            # minimal 模板无 gate_rules → 直接 auto_passed
            check("fix 阶段 gate_status=auto_passed", fix_task.get("gate_status") == "auto_passed",
                  str(fix_task.get("gate_status")))
            check("fix 阶段结果入库", "最终答案" in (fix_task.get("result") or ""),
                  (fix_task.get("result") or "")[:60])

        # ── W3 workflow 完成 ──
        print("\n[W3] workflow 完成")
        r = wait_status(run_id, {"completed"}, timeout=90)
        check("workflow completed", r is not None and r["status"] == "completed",
              r and r["status"])

        # ── W4 单任务共存 ──
        print("\n[W4] 单任务入口共存")
        t = api("POST", "/api/tasks", {"title": "single", "description": "正常任务",
                                       "repo_path": str(tmp)})
        done = None
        deadline = time.time() + 60
        while time.time() < deadline:
            tt = api("GET", f"/api/tasks/{t['id']}")
            if tt["status"] in ("completed", "failed"):
                done = tt
                break
            time.sleep(1)
        check("单任务完成", done and done["status"] == "completed", done and done["status"])

        # ── 产物 commit 检查 ──
        print("\n[W5] 阶段产物 git commit")
        # mock pi 不产生产物文件 → git commit 应为 "nothing to commit"（安全处理，不崩溃）
        daemon_log.flush()
        log_path = Path(daemon_log.name)
        daemon_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        check("daemon 无异常崩溃", "Traceback" not in daemon_text,
              daemon_text[-300:] if "Traceback" in daemon_text else "")

    finally:
        if daemon and daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        server.kill()
        server.wait()
        server_log.close()
        daemon_log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*50}\nWorkflow E2E 结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
