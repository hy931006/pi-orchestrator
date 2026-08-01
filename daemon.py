#!/usr/bin/env python3
"""
Pi Orchestrator Daemon — 独立进程
像素级复刻 Multica daemon 的核心调度行为:
  - 启动时僵尸任务恢复（claimed/running → queued，对应 reconcile）
  - 检测 PATH 上的 agent CLI → 注册 runtime → 心跳
  - 轮询 SQLite 任务队列 → 原子 claim → 线程池并发执行（真 MAX_CONCURRENT）
  - 取消请求轮询：kill 对应 Pi 进程树 → status=cancelled
  - Git Worktree 隔离 + Pi JSON 事件流解析
  - 优雅关停：停止领取新任务，等待在途任务结束
"""
import subprocess
import socket
import time
import sys
import signal
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import database as db
from agent import PiAgent, detect_pi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s: %(message)s"
)
logger = logging.getLogger("daemon")

# ── 配置 ──
POLL_INTERVAL = 3
HEARTBEAT_INTERVAL = 15
MAX_CONCURRENT = 3
TASK_TIMEOUT = 7200
WORKTREE_ROOT = Path.home() / ".pi-orchestrator" / "worktrees"
# daemon 实例 id：hostname + pid，避免同机多实例互相冒领
DAEMON_ID = f"{socket.gethostname()}:{__import__('os').getpid()}"


# ═══════════════════════════════════════════
# Git Worktree
# ═══════════════════════════════════════════

def create_worktree(repo_path: str, task_id: str) -> str | None:
    """创建 git worktree 隔离工作区"""
    worktree_name = f"task-{task_id[:8]}"
    worktree_dir = WORKTREE_ROOT / worktree_name
    branch_name = f"orchestrator/{worktree_name}"

    if worktree_dir.exists():
        cleanup_worktree(str(worktree_dir), repo_path)

    worktree_dir.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "add", "-b", branch_name, str(worktree_dir)],
            capture_output=True, check=True, timeout=30
        )
        logger.info(f"🌲 Worktree: {worktree_dir}")
        return str(worktree_dir)
    except subprocess.CalledProcessError:
        # 分支存在 → 换名
        branch_name += f"-{int(time.time())}"
        try:
            subprocess.run(
                ["git", "-C", repo_path, "worktree", "add", "-b", branch_name, str(worktree_dir)],
                capture_output=True, check=True, timeout=30
            )
            return str(worktree_dir)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Worktree failed: {e.stderr}")
            return None


def cleanup_worktree(worktree_path: str, repo_path: str):
    try:
        subprocess.run(["git", "-C", repo_path, "worktree", "remove", "--force", worktree_path],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def is_git_repo(path: str) -> bool:
    try:
        subprocess.run(["git", "-C", path, "rev-parse", "--git-dir"],
                       capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════
# Runtime 管理
# ═══════════════════════════════════════════

class RuntimeManager:
    """注册 agent 运行时，定期心跳"""

    def __init__(self, daemon_id: str):
        self.daemon_id = daemon_id
        self.runtimes: dict[str, str] = {}  # agent_type → runtime_id
        self._running = False

    def register(self, agent_type: str, binary_path: str):
        rid = db.register_runtime(self.daemon_id, agent_type, binary_path)
        self.runtimes[agent_type] = rid
        logger.info(f"📡 Registered runtime: {agent_type} → {rid}")

    def mark_offline(self):
        """关停时主动下线（对应 Go daemon 的 shutdown 上报）"""
        for rid in self.runtimes.values():
            try:
                with db.get_db() as conn:
                    conn.execute("UPDATE runtimes SET is_online=0 WHERE id=?", (rid,))
            except Exception:
                pass

    def start_heartbeat(self):
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="heartbeat")
        t.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                for rid in self.runtimes.values():
                    db.heartbeat_runtime(rid)
                db.mark_runtimes_offline(timeout_minutes=2)
            except Exception as e:
                logger.warning(f"heartbeat error: {e}")
            time.sleep(HEARTBEAT_INTERVAL)


# ═══════════════════════════════════════════
# Task Executor
# ═══════════════════════════════════════════

class TaskExecutor:
    """执行单个 Pi 任务（在池内线程中运行）"""

    def __init__(self, pi_path: str):
        self.pi_path = pi_path
        self.agent = PiAgent(pi_path, timeout=TASK_TIMEOUT)
        # task_id → cancel_event，供取消轮询线程定位
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            ev = self._cancel_events.get(task_id)
        if ev:
            ev.set()
            return True
        return False

    def execute(self, task_id: str):
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[task_id] = cancel_event
        try:
            self._execute_inner(task_id, cancel_event)
        finally:
            with self._lock:
                self._cancel_events.pop(task_id, None)

    def _execute_inner(self, task_id: str, cancel_event: threading.Event):
        task = db.get_task(task_id)
        if not task:
            return

        prompt = task.get("description") or task.get("title", "")
        repo_path = task.get("repo_path") or str(Path.cwd())

        # ── Agent 人格注入（复刻 Multica：system prompt 走 --append-system-prompt，
        #    不拼进用户 prompt；model 走 --provider/--model）──
        system_prompt = ""
        model = task.get("model") or ""
        agent_id = task.get("agent_type")
        if agent_id and agent_id != "auto":
            agent = db.get_agent(agent_id)
            if agent:
                system_prompt = agent.get("system_prompt") or ""
                if not model:
                    model = agent.get("model") or ""
                logger.info(f"🤖 Agent [{agent['name']}] system_prompt={len(system_prompt)}B model={model or 'default'}")

        db.update_task_status(task_id, "running")
        logger.info(f"🚀 [{task_id}] {task['title'][:50]}")

        # ── Git Worktree ──
        if is_git_repo(repo_path):
            worktree = create_worktree(repo_path, task_id)
            if worktree:
                db.update_task_status(task_id, "running", worktree_path=worktree)
                cwd = worktree
            else:
                logger.warning(f"[{task_id}] worktree 创建失败，回退到原仓库目录执行")
                cwd = repo_path
        else:
            cwd = repo_path

        # ── Execute Pi（cancel_event 支持外部中止）──
        result = self.agent.execute(
            prompt=prompt,
            cwd=cwd,
            model=model,
            system_prompt=system_prompt,
            resume_session_id=task.get("session_id") or "",
            cancel_event=cancel_event,
        )

        # ── Build log ──
        log_parts = []
        if result.thinking:
            log_parts.append(f"[THINKING]\n{result.thinking}\n[/THINKING]\n")
        if result.text:
            log_parts.append(result.text)
        if result.tool_calls:
            log_parts.append(f"\n[TOOLS: {len(result.tool_calls)} calls]")
        if result.usage:
            usage_str = "; ".join(
                f"{m}: in={u['input_tokens']} out={u['output_tokens']}"
                for m, u in result.usage.items())
            log_parts.append(f"\n[USAGE: {usage_str}]")
        if result.errors:
            log_parts.append(f"\n[ERRORS: {'; '.join(result.errors)}]")
        log = "".join(log_parts).strip()

        # ── 终态映射（completed/failed/timeout/aborted → 任务状态）──
        if result.status == "completed":
            status = "completed"
            result_text = result.text  # 修复：成功时存真实输出，而非 "Exit code: 0"
        elif result.status == "aborted":
            # 若用户在执行中先点了 block，任务已是 blocked —— 保留 blocked，不覆盖为 cancelled
            current = db.get_task(task_id)
            if current and current["status"] == "blocked":
                status = "blocked"
            else:
                status = "cancelled"
            result_text = "execution cancelled"
        elif result.status == "timeout":
            status = "failed"
            result_text = result.error
        else:
            status = "failed"
            result_text = result.error or f"Exit code: {result.exit_code}"

        db.update_task_status(task_id, status, log=log, result=result_text,
                              session_id=result.session_id)
        logger.info(f"{'✅' if status == 'completed' else '❌'} [{task_id}] {status}: "
                    f"{(result.text or result.error)[:80]}")


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main():
    logger.info("🔄 Pi Orchestrator Daemon starting...")
    db.init_db()

    pi_path = detect_pi()
    if not pi_path:
        logger.error("❌ Pi not found on PATH. Exiting.")
        return

    # ── 僵尸任务恢复（对应 Multica reconcile：崩溃 daemon 留下的 claimed/running 重新入队）──
    stale = db.requeue_stale_tasks()  # 单 daemon 部署：恢复全部
    if stale:
        logger.warning(f"♻️ 恢复 {len(stale)} 个僵尸任务 → queued: {', '.join(stale)}")
        for tid in stale:
            db.add_comment(tid, "♻️ Daemon 重启，任务重新入队", "system")

    # Runtime 注册
    runtime_mgr = RuntimeManager(DAEMON_ID)
    runtime_mgr.register("pi", pi_path)
    runtime_mgr.start_heartbeat()

    executor = TaskExecutor(pi_path)
    pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="task")
    running = True
    in_flight: dict = {}  # future → task_id

    # ── 取消请求轮询线程 ──
    def cancel_watcher():
        while running:
            try:
                for tid in db.list_cancel_requested():
                    if executor.cancel_task(tid):
                        logger.info(f"🛑 取消任务 [{tid}]")
                    else:
                        # 已 claim 但还没开始执行 → 直接标记取消
                        task = db.get_task(tid)
                        if task and task["status"] == "claimed":
                            db.update_task_status(tid, "cancelled", result="execution cancelled")
                            db.clear_cancel(tid)
            except Exception as e:
                logger.warning(f"cancel watcher error: {e}")
            time.sleep(1)

    watcher = threading.Thread(target=cancel_watcher, daemon=True, name="cancel-watcher")
    watcher.start()

    def shutdown(sig, frame):
        nonlocal running
        running = False
        logger.info("⏹️ Shutting down... (等待在途任务结束，Ctrl+C 再次按下强制退出)")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info(f"Polling every {POLL_INTERVAL}s | Heartbeat every {HEARTBEAT_INTERVAL}s | "
                f"Max {MAX_CONCURRENT} concurrent | daemon_id={DAEMON_ID}")
    logger.info("Press Ctrl+C to stop")

    while running:
        try:
            # 清理已完成的 future
            done = [f for f in in_flight if f.done()]
            for f in done:
                exc = f.exception()
                if exc:
                    logger.exception(f"任务线程异常: {exc}")
                in_flight.pop(f, None)

            if len(in_flight) >= MAX_CONCURRENT:
                time.sleep(POLL_INTERVAL)
                continue

            task = db.get_next_queued_task()
            if not task:
                time.sleep(POLL_INTERVAL)
                continue

            task_id = task["id"]
            if not db.claim_task(task_id, DAEMON_ID):
                continue

            future = pool.submit(executor.execute, task_id)
            in_flight[future] = task_id

        except Exception as e:
            logger.exception(f"Loop error: {e}")
            time.sleep(POLL_INTERVAL)

    # ── 优雅关停：不再领新任务，等在途任务完成 ──
    runtime_mgr.stop()
    if in_flight:
        logger.info(f"等待 {len(in_flight)} 个在途任务完成...")
        pool.shutdown(wait=True)
    else:
        pool.shutdown(wait=False)
    runtime_mgr.mark_offline()
    logger.info("Daemon stopped.")


if __name__ == "__main__":
    main()
