#!/usr/bin/env python3
"""
端到端测试 — server + daemon + mock pi shim 完整调度链路。
在 Windows 上构造 pi.cmd + pi.ps1 shim，走真实的 choose_pi_invocation 重写路径 (#3306)。

验证:
  E1 任务全生命周期 queued→claimed→running→completed
  E2 MAX_CONCURRENT=3 真并发（3 个 4 秒任务总耗时应远小于串行 12 秒）
  E3 取消运行中任务 → cancelled
  E4 daemon 崩溃 → 僵尸任务重启后自动恢复并完成
  E5 error 事件 → failed 且 error 入库

运行: python tests/test_e2e.py
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
PORT = 8123
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


def wait_task(task_id, want_statuses, timeout=60):
    """轮询任务直到进入目标状态集合，返回任务 dict 或 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = api("GET", f"/api/tasks/{task_id}")
        if t["status"] in want_statuses:
            return t
        time.sleep(1)
    return None


def make_shim(dir_):
    """构造 pi.cmd + pi.ps1 shim（Windows #3306 路径）或 POSIX pi 脚本"""
    if sys.platform == "win32":
        cmd = dir_ / "pi.cmd"
        ps1 = dir_ / "pi.ps1"
        ps1.write_text(f'& "{sys.executable}" "$PSScriptRoot\\mock_pi.py" @args\nexit $LASTEXITCODE\n',
                       encoding="utf-8")
        cmd.write_text(f'@echo off\n"{sys.executable}" "%~dp0mock_pi.py" %*\n', encoding="utf-8")
        shutil.copy(MOCK_PI, dir_ / "mock_pi.py")
    else:
        sh = dir_ / "pi"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{dir_}/mock_pi.py" "$@"\n')
        sh.chmod(0o755)
        shutil.copy(MOCK_PI, dir_ / "mock_pi.py")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pio-e2e-"))
    shim_dir = tmp / "bin"
    shim_dir.mkdir()
    make_shim(shim_dir)

    env = os.environ.copy()
    env["PI_ORCHESTRATOR_DB"] = str(tmp / "test.db")
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["PI_MOCK_ARGV_FILE"] = str(tmp / "argv.jsonl")

    # 子进程日志写文件，避免 PIPE 缓冲填满导致 server/daemon 阻塞
    server_log = open(tmp / "server.log", "w", encoding="utf-8")
    daemon_log = open(tmp / "daemon.log", "w", encoding="utf-8")

    server = subprocess.Popen([sys.executable, "run.py", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, env=env,
                              stdout=server_log, stderr=subprocess.STDOUT)
    daemon = None
    try:
        # 等 server 起来
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

        # ── E1 正常任务 ──
        print("\n[E1] 任务全生命周期")
        t = api("POST", "/api/tasks", {"title": "E1 正常任务", "description": "写个 hello world",
                                       "repo_path": str(tmp)})
        done = wait_task(t["id"], {"completed", "failed"}, timeout=60)
        check("任务完成", done and done["status"] == "completed", done and done["status"])
        check("result 是真实输出（非 Exit code: 0）",
              done and "最终答案：42" in (done.get("result") or ""), done and repr(done.get("result"))[:80])
        check("log 含 thinking", done and "[THINKING]" in (done.get("log") or ""))
        check("session_id 入库", done and (done.get("session_id") or "").endswith(".jsonl"))

        # ── E2 真并发 ──
        print("\n[E2] MAX_CONCURRENT=3 真并发")
        ids = []
        t0 = time.time()
        for i in range(3):
            r = api("POST", "/api/tasks", {"title": f"E2 并发 {i}", "description": "SLOW=4 并发测试",
                                           "repo_path": str(tmp)})
            ids.append(r["id"])
        results = [wait_task(i, {"completed", "failed"}, timeout=60) for i in ids]
        elapsed = time.time() - t0
        check("3 个任务全部完成", all(r and r["status"] == "completed" for r in results),
              str([r and r["status"] for r in results]))
        check(f"并发执行（{elapsed:.0f}s < 串行 12s）", elapsed < 12, f"{elapsed:.1f}s")

        # ── E3 取消运行中任务 ──
        print("\n[E3] 取消运行中任务")
        t = api("POST", "/api/tasks", {"title": "E3 长任务", "description": "SLOW=60 等待取消",
                                       "repo_path": str(tmp)})
        running = wait_task(t["id"], {"running"}, timeout=30)
        check("任务进入 running", running is not None)
        api("PUT", f"/api/tasks/{t['id']}/cancel")
        cancelled = wait_task(t["id"], {"cancelled"}, timeout=30)
        check("任务被取消", cancelled is not None, cancelled and cancelled["status"])

        # ── E5 error 事件 → failed ──
        print("\n[E5] error 事件 → failed")
        t = api("POST", "/api/tasks", {"title": "E5 失败任务", "description": "FAIL 这个任务",
                                       "repo_path": str(tmp)})
        failed = wait_task(t["id"], {"failed"}, timeout=60)
        check("任务 failed", failed is not None)
        check("error 入库", failed and "provider overloaded" in (failed.get("result") or ""),
              failed and repr(failed.get("result"))[:80])

        # ── E4 僵尸任务恢复 ──
        print("\n[E4] daemon 崩溃 → 僵尸任务恢复")
        t = api("POST", "/api/tasks", {"title": "E4 僵尸任务", "description": "SLOW=60 僵尸测试",
                                       "repo_path": str(tmp)})
        running = wait_task(t["id"], {"running"}, timeout=30)
        check("任务进入 running", running is not None)
        daemon.kill()   # 模拟崩溃（不等优雅退出）
        daemon.wait()
        t2 = api("GET", f"/api/tasks/{t['id']}")
        check("崩溃后任务卡在 running", t2["status"] in ("running", "claimed"), t2["status"])
        # 把 mock 换成快速模式：直接改 shim 的 mock 输出？简单起见，新 daemon 恢复后会重新执行
        # SLOW=60 → 60s 太慢；改为等待 daemon 将其恢复为 queued，再手动改 DB 不可行。
        # 验证恢复为 queued 后，重启的 daemon 会重新执行它（等待完成，最多 90s）
        daemon2 = subprocess.Popen([sys.executable, "daemon.py"], cwd=ROOT, env=env,
                                   stdout=daemon_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 30
            requeued = False
            while time.time() < deadline:
                tt = api("GET", f"/api/tasks/{t['id']}")
                if tt["status"] in ("queued", "running", "completed"):
                    requeued = True
                    break
                time.sleep(1)
            check("僵尸任务被恢复", requeued, tt["status"])
            # 等待其最终完成（SLOW=60，给 90s）
            done = wait_task(t["id"], {"completed", "failed", "cancelled"}, timeout=90)
            check("恢复后任务最终完成", done and done["status"] == "completed", done and done["status"])
        finally:
            daemon2.kill()
            daemon2.wait()

        # ── 统计面板 ──
        stats = api("GET", "/api/stats")
        check("stats 含 cancelled 计数", "cancelled" in stats and stats["cancelled"] >= 1, str(stats))
        check("stats 一致性", stats["completed"] >= 5, str(stats))

    finally:
        server.kill()
        if daemon and daemon.poll() is None:
            daemon.kill()
        server.wait()
        server_log.close()
        daemon_log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*50}\nE2E 结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
