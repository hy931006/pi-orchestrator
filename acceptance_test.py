#!/usr/bin/env python3
"""
acceptance_test.py — 真实 pi 全链路验收脚本（wf-001 最终验证）

在【真实终端】运行（不是 Hermes background！daemon 需要捕获子进程 stdout）:

    python acceptance_test.py

流程:
  1. 检测 pi CLI 可用性
  2. 建临时 git 仓库 + 临时 DB（不污染 orchestrator.db）
  3. 启动 server (run.py) + daemon (daemon.py)
  4. 通过 API 创建 minimal workflow（真实 pi 执行 fix → verify 两阶段）
  5. 轮询等待: 阶段执行 → gate → 自动流转 → workflow completed
  6. 打印验收报告 + 清理

退出码: 0 = 全部通过, 1 = 任一失败

环境变量:
  PI_EXECUTABLE   覆盖 pi 路径（默认 PATH 检测）
  ACCEPT_TIMEOUT  单阶段超时秒数（默认 300）
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

ROOT = Path(__file__).resolve().parent
PORT = 8150
BASE = f"http://127.0.0.1:{PORT}"
STAGE_TIMEOUT = int(os.environ.get("ACCEPT_TIMEOUT", "300"))

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
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def wait_workflow(run_id, want, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = api("GET", f"/api/workflows/{run_id}")
        except Exception:
            time.sleep(2)
            continue
        if r["status"] in want:
            return r
        time.sleep(2)
    return None


def main():
    print("=" * 60)
    print("Pi Orchestrator — 真实 pi 全链路验收")
    print("=" * 60)

    # ── 1. pi 检测 ──
    print("\n[1] 环境检测")
    pi_path = os.environ.get("PI_EXECUTABLE") or shutil.which("pi")
    check("pi CLI 可用", pi_path is not None, "PATH 中未找到 pi，请先安装或设置 PI_EXECUTABLE")
    if not pi_path:
        sys.exit(1)
    print(f"    pi → {pi_path}")

    try:
        ver = subprocess.run([pi_path, "--version"], capture_output=True,
                             text=True, timeout=30)
        print(f"    version → {(ver.stdout or ver.stderr).strip()[:60]}")
    except Exception as e:
        print(f"    ⚠️ version 获取失败: {e}（不影响验收）")

    # ── 2. 临时环境 ──
    print("\n[2] 准备临时环境")
    tmp = Path(tempfile.mkdtemp(prefix="accept-"))
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo])
    subprocess.run(["git", "-C", repo, "config", "user.email", "accept@test.local"])
    subprocess.run(["git", "-C", repo, "config", "user.name", "acceptance"])

    env = os.environ.copy()
    env["PI_ORCHESTRATOR_DB"] = str(tmp / "accept.db")
    env["PI_EXECUTABLE"] = str(pi_path)

    server_log = open(tmp / "server.log", "w", encoding="utf-8")
    daemon_log = open(tmp / "daemon.log", "w", encoding="utf-8")
    server = daemon = None

    try:
        # ── 3. 启动 server + daemon ──
        print("\n[3] 启动 server + daemon")
        server = subprocess.Popen([sys.executable, "run.py", "--host", "127.0.0.1", "--port", str(PORT)],
                                  cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
        for _ in range(30):
            try:
                api("GET", "/health")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("server 启动失败（见 server.log）")

        daemon = subprocess.Popen([sys.executable, "daemon.py"], cwd=ROOT, env=env,
                                  stdout=daemon_log, stderr=subprocess.STDOUT)
        time.sleep(3)
        check("server 健康", True)
        check("daemon 启动（3s 后未崩溃）", daemon.poll() is None,
              "daemon 退出，见 daemon.log")

        # ── 4. 创建 workflow（真实 pi 执行 minimal 两阶段）──
        print("\n[4] 创建 workflow (minimal, 真实 pi)")
        w = api("POST", "/api/workflows", {
            "template_name": "minimal",
            "title": "acceptance real-pi run",
            "repo_path": str(repo),
        })
        run_id = w["run"]["id"]
        check("workflow 创建", run_id and w["run"]["current_stage"] == "fix", str(w))

        # ── 5. 等待全流程完成 ──
        print(f"\n[5] 等待阶段执行（每阶段 ≤{STAGE_TIMEOUT}s，真实 pi 调用）")
        total_deadline = time.time() + STAGE_TIMEOUT * 2 + 120
        final = None
        while time.time() < total_deadline:
            try:
                r = api("GET", f"/api/workflows/{run_id}")
            except Exception:
                time.sleep(2)
                continue
            if r["status"] in ("completed", "failed"):
                final = r
                break
            # 打印进度
            stages = api("GET", f"/api/workflows/{run_id}/stages")["stages"]
            statuses = [(s["stage_key"], s["status"], s.get("gate_status") or "-")
                        for s in stages]
            print(f"    ⏳ {statuses}")
            time.sleep(5)

        if final is None:
            check("workflow 完成", False, "超时（见 daemon.log 排查）")
        else:
            check("workflow 状态", final["status"] == "completed", final["status"])

        # ── 6. 阶段详情验证 ──
        print("\n[6] 阶段验证")
        stages = api("GET", f"/api/workflows/{run_id}/stages")["stages"]
        check("两阶段任务齐备", len(stages) == 2, f"{len(stages)} stages")

        if stages:
            fix_task = stages[0]
            check("fix 阶段 gate_status", fix_task.get("gate_status") == "auto_passed",
                  str(fix_task.get("gate_status")))
            result_text = fix_task.get("result") or ""
            check("fix 阶段有真实输出", len(result_text.strip()) > 0,
                  result_text[:80])

        # ── 7. 产物 commit ──
        print("\n[7] 产物追溯")
        if final and final["status"] == "completed":
            logs = subprocess.run(["git", "-C", repo, "log", "--oneline"],
                                  capture_output=True, text=True).stdout
            check("存在阶段产物 commit", "stage artifacts" in logs, logs[:200])

        # ── 8. daemon 日志无崩溃 ──
        daemon_log.flush()
        dtext = Path(daemon_log.name).read_text(encoding="utf-8", errors="replace")
        check("daemon 无异常崩溃", "Traceback" not in dtext,
              dtext[-300:] if "Traceback" in dtext else "")

    finally:
        if daemon and daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        if server and server.poll() is None:
            server.kill()
            server.wait()
        server_log.close()
        daemon_log.close()
        if not os.environ.get("ACCEPT_KEEP_TMP"):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n临时目录保留: {tmp}（ACCEPT_KEEP_TMP=1）")

    print("\n" + "=" * 60)
    print(f"验收结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
