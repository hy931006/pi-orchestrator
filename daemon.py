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
import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import database as db
from agent import detect_pi
from backends import create_backend, list_backends
import gate
import workflow as wf

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
    """执行单个 Agent 任务（在池内线程中运行）

    backend 通过工厂获取（默认 pi，可用 PI_ORCHESTRATOR_BACKEND 切换，
    如 echo / claude-code / codex —— 见 backends/ 包）。
    """

    def __init__(self, backend_name: str = None, backend_kwargs: dict = None):
        self.backend_name = backend_name or "pi"
        self.agent = create_backend(self.backend_name,
                                    **(backend_kwargs or {}),
                                    timeout=TASK_TIMEOUT)
        logger.info(f"🤖 执行 backend: {self.backend_name} ({type(self.agent).__name__})")
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

        # ── workflow 阶段完成后的门控 + 流转 ──
        if status == "completed":
            self._after_stage(task, result)

    def _after_stage(self, task: dict, result):
        """workflow 阶段任务完成后的门控 + 流转（设计 §5.1/§6）"""
        run_id = task.get("workflow_run_id")
        if not run_id:
            return
        run = db.get_workflow_run(run_id)
        if not run:
            logger.error(f"workflow {run_id}: 实例不存在")
            return
        try:
            templates = wf.load_templates()
        except wf.WorkflowError as e:
            logger.error(f"workflow {run_id}: 模板加载失败: {e}")
            return
        template = templates.get(run["template_name"])
        if not template:
            logger.error(f"workflow {run_id}: 模板 {run['template_name']} 丢失")
            return
        stages = template["stages"]
        stage = stages[task.get("stage_index") or 0]
        stage_key = task.get("stage_key") or stage["key"]
        # repair 节点重开时 stage_index 可能错位——以 stage_key 为准找定义
        stage = next((s for s in stages if s["key"] == stage_key), stage)
        is_repair = stage.get("type") == "repair"
        # 共享 repair 节点：父阶段以任务上的 repair_for 为准，兜底模板扫描
        parent = None
        if is_repair:
            pkey = task.get("repair_for")
            if pkey:
                parent = next((s for s in stages if s["key"] == pkey), None)
            if not parent:
                parent = wf.repair_parent(template, stage_key)

        # ── gate 自动检查 ──
        gate_status = "auto_passed"
        gate_detail = {"stage": stage_key}
        # ui_design 节点：跑 impeccable detect 做 UI 质量门控
        if not is_repair and stage.get("type") == "ui_design":
            ui_target = stage.get("ui_target") or "templates"
            detect_passed, detect_summary = self._run_ui_detect(ui_target)
            gate_status = "auto_passed" if detect_passed else "auto_failed"
            gate_detail["ui_detect"] = detect_summary
            logger.info(f"🎨 [{task['id']}] UI detect={'✅' if detect_passed else '❌'} "
                        f"({detect_summary})")
        # repair 节点复用父阶段的 gate_rules + 父阶段产物目录（R1 自旋）
        gate_src = parent if (is_repair and parent) else stage
        rules_rel = gate_src.get("gate_rules", "")
        if rules_rel:
            artifact_dir = Path(run["artifact_dir"]) / gate_src["key"]
            rules_path = Path(run["artifact_dir"]) / rules_rel
            try:
                passed, md = gate.run_gate(rules_path, artifact_dir)
                gate_status = "auto_passed" if passed else "auto_failed"
                logger.info(f"🔒 [{task['id']}] gate={gate_status} ({'✅' if passed else '❌'})")
            except gate.GateError as e:
                gate_status = "auto_failed"
                logger.error(f"🔒 [{task['id']}] gate 异常 → auto_failed: {e}")
        elif not is_repair and not stage.get("type") == "ui_design":
            logger.info(f"🔒 [{task['id']}] 无 gate_rules，直接 auto_passed")

        db.update_task_status(task["id"], "completed",
                              gate_status=gate_status,
                              gate_result_json=json.dumps(gate_detail, ensure_ascii=False))

        # ── 阶段产物 git commit（设计 §5.3）──
        self._commit_stage_artifacts(run, stage_key)

        # ── repair 节点：结果路由（通过→回补父阶段+流转；未过→重试/转人工）──
        if is_repair:
            action = wf.handle_repair_result(run, db.get_task(task["id"]), template,
                                             passed=(gate_status == "auto_passed"))
            if action == "unlocked":
                wf.unlock_next_stages(run_id)
            return

        # ── 主线节点 gate 失败：repair 路由，或停住等人工（不再无条件流转）──
        if gate_status == "auto_failed":
            repair_task = wf.route_gate_failure(run, task, template)
            if not repair_task:
                logger.info(f"⏸️ [{task['id']}] gate 失败且无 repair 路由 → "
                            f"停在 {stage_key} 等人工 (reject/force/waive)")
            return

        # ── 自动流转（DAG 依赖解锁，支持并行分支）──
        try:
            wf.unlock_next_stages(run_id)
        except wf.WorkflowError as e:
            logger.error(f"workflow {run_id}: 流转失败: {e}")

    def _run_ui_detect(self, target: str) -> tuple:
        """运行 npx impeccable detect 检查 UI 反模式。返回 (通过?, 摘要)"""
        try:
            # Windows 上 npx 是 .cmd，subprocess 需经 cmd /c 或完整路径
            npx = shutil.which("npx") or shutil.which("npx.cmd")
            if not npx:
                return False, "npx 未找到（需要 Node.js）"
            if sys.platform == "win32" and npx.lower().endswith(".cmd"):
                cmd = ["cmd", "/c", "npx", "-y", "impeccable", "detect", target]
            else:
                cmd = [npx, "-y", "impeccable", "detect", target]
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180, cwd=Path.cwd())
            out = (r.stdout or "") + (r.stderr or "")
            # 统计反模式数：最后一行 "N anti-patterns found." 或 0 个
            import re as _re
            m = _re.search(r"(\d+)\s+anti-patterns? found", out)
            count = int(m.group(1)) if m else (0 if r.returncode == 0 else -1)
            return count == 0, f"{count} anti-patterns"
        except Exception as e:
            logger.warning(f"impeccable detect 执行异常: {e}")
            return False, f"detect 异常: {e}"

    def _commit_stage_artifacts(self, run: dict, stage_key: str):
        """阶段产物 commit（每个阶段一个独立 commit，Q6 追溯）"""
        repo = run.get("repo_path")
        if not repo:
            return
        try:
            subprocess.run(["git", "-C", repo, "add", run["artifact_dir"]],
                           capture_output=True, timeout=30)
            r = subprocess.run(
                ["git", "-C", repo, "commit", "-m",
                 f"docs({run['id']}/{stage_key}): stage artifacts"],
                capture_output=True, timeout=30)
            if r.returncode != 0 and b"nothing to commit" not in r.stderr:
                logger.warning(f"stage commit 失败: {r.stderr.decode(errors='replace')[:200]}")
        except Exception as e:
            logger.warning(f"stage commit 异常: {e}")


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main():
    logger.info("🔄 Pi Orchestrator Daemon starting...")
    db.init_db()

    # ── workflow 模板预加载（R9: 语法错误 → 拒绝启动）──
    try:
        templates = wf.load_templates()
        logger.info(f"📂 加载 workflow 模板: {sorted(templates)}")
    except wf.WorkflowError as e:
        logger.error(f"❌ workflow 模板加载失败，拒绝启动: {e}")
        return

    # ── 执行 backend 选择（默认 pi；可用 PI_ORCHESTRATOR_BACKEND 切换）──
    import os as _os
    backend_name = _os.environ.get("PI_ORCHESTRATOR_BACKEND", "pi")
    logger.info(f"🤖 执行 backend: {backend_name}（可用: {list_backends()}）")

    backend_kwargs = {}
    if backend_name == "pi":
        pi_path = detect_pi()
        if not pi_path:
            logger.error("❌ Pi not found on PATH. Exiting.")
            return
        backend_kwargs["pi_path"] = pi_path
    # 其他 backend：不要求 pi 存在（如 echo 直接可用）

    # ── 僵尸任务恢复（对应 Multica reconcile：崩溃 daemon 留下的 claimed/running 重新入队）──
    stale = db.requeue_stale_tasks()  # 单 daemon 部署：恢复全部
    if stale:
        logger.warning(f"♻️ 恢复 {len(stale)} 个僵尸任务 → queued: {', '.join(stale)}")
        for tid in stale:
            db.add_comment(tid, "♻️ Daemon 重启，任务重新入队", "system")

    # Runtime 注册
    runtime_mgr = RuntimeManager(DAEMON_ID)
    runtime_mgr.register(backend_name, backend_kwargs.get("pi_path") or backend_name)
    runtime_mgr.start_heartbeat()

    executor = TaskExecutor(backend_name=backend_name, backend_kwargs=backend_kwargs)
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
