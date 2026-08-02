"""
SQLite 数据库模块 — 任务队列 + Agent 运行时管理
"""
import sqlite3
import os
import uuid
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

# 可用 PI_ORCHESTRATOR_DB 环境变量覆盖（测试隔离 / 多实例）
DB_PATH = Path(os.environ.get("PI_ORCHESTRATOR_DB", Path(__file__).parent / "orchestrator.db"))


@contextmanager
def get_db():
    """获取数据库连接（上下文管理器，自动关闭）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """初始化数据库表"""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                priority TEXT DEFAULT 'medium',
                agent_type TEXT,
                repo_path TEXT,
                branch TEXT,
                worktree_path TEXT,
                result TEXT,
                log TEXT DEFAULT '',
                claimed_by TEXT,
                blocked_reason TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                author TEXT DEFAULT 'user',
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS runtimes (
                id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                binary_path TEXT,
                version TEXT,
                is_online BOOLEAN DEFAULT 1,
                last_heartbeat TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS daemon_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                is_running BOOLEAN DEFAULT 1,
                last_poll TEXT,
                running_tasks INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                system_prompt TEXT DEFAULT '',
                model TEXT DEFAULT '',
                skills TEXT DEFAULT '[]',
                tools TEXT DEFAULT '[]',
                is_active BOOLEAN DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            INSERT OR IGNORE INTO daemon_state (id, is_running) VALUES (1, 1);
        """)
        _migrate_tasks(conn)
        _migrate_v1_to_v2(conn)


def _migrate_tasks(conn):
    """轻量列迁移：为已存在的库补新列（SQLite 无 IF NOT EXISTS for ADD COLUMN）"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    migrations = {
        "session_id": "ALTER TABLE tasks ADD COLUMN session_id TEXT",
        "cancel_requested": "ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER DEFAULT 0",
        "model": "ALTER TABLE tasks ADD COLUMN model TEXT DEFAULT ''",
    }
    for col, ddl in migrations.items():
        if col not in existing:
            conn.execute(ddl)


def _migrate_v1_to_v2(conn):
    """v1 → v2: workflow 编排层迁移（纯增补，零数据风险）"""
    existing_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "workflow_runs" not in existing_tables:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                template_name TEXT NOT NULL,
                title TEXT NOT NULL,
                repo_path TEXT,
                branch TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                current_stage TEXT,
                current_stage_index INTEGER DEFAULT 0,
                artifact_dir TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for col, ddl in [
        ("workflow_run_id", "ALTER TABLE tasks ADD COLUMN workflow_run_id TEXT"),
        ("stage_key", "ALTER TABLE tasks ADD COLUMN stage_key TEXT"),
        ("stage_index", "ALTER TABLE tasks ADD COLUMN stage_index INTEGER"),
        ("gate_status", "ALTER TABLE tasks ADD COLUMN gate_status TEXT"),
        ("gate_result_json", "ALTER TABLE tasks ADD COLUMN gate_result_json TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(ddl)


# ────────────────────────────────
# Workflow Runs CRUD
# ────────────────────────────────

def create_workflow_run(template_name: str, title: str, repo_path: str = None) -> dict:
    """创建 workflow 实例"""
    run_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO workflow_runs (id, template_name, title, repo_path, artifact_dir) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, template_name, title, repo_path or str(Path.cwd()),
             f"docs/{run_id}"))
    return get_workflow_run(run_id)


def get_workflow_run(run_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_workflow_runs(status: str = None, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM workflow_runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_workflow_run(run_id: str, **kwargs):
    allowed = {"status", "current_stage", "current_stage_index", "branch"}
    fields, values = [], []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            values.append(v)
    if not fields:
        return
    fields.append("updated_at = datetime('now','localtime')")
    values.append(run_id)
    with get_db() as conn:
        conn.execute(f"UPDATE workflow_runs SET {', '.join(fields)} WHERE id = ?", values)


# ────────────────────────────────
# Task CRUD
# ────────────────────────────────

def create_task(title: str, description: str = "", agent_type: str = "auto",
                repo_path: str = None) -> dict:
    """创建新任务"""
    task_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO tasks (id, title, description, agent_type, repo_path)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, title, description, agent_type, repo_path or str(Path.cwd()))
        )
    return get_task(task_id)


def get_task(task_id: str) -> Optional[dict]:
    """获取单个任务"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(status: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """列出任务，可按状态过滤"""
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
    return [dict(r) for r in rows]


def claim_task(task_id: str, daemon_id: str) -> bool:
    """Daemon 认领任务（原子操作，防止重复认领）"""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE tasks SET status='claimed', claimed_by=?, cancel_requested=0,
               updated_at=datetime('now','localtime')
               WHERE id = ? AND status = 'queued'""",
            (daemon_id, task_id)
        )
        return cursor.rowcount > 0


def update_task_status(task_id: str, status: str, **kwargs):
    """更新任务状态"""
    valid = {'queued', 'claimed', 'running', 'completed', 'failed', 'blocked', 'cancelled'}
    if status not in valid:
        raise ValueError(f"Invalid status: {status}")

    fields = ["status = ?", "updated_at = datetime('now','localtime')"]
    values = [status]

    for key in ('log', 'result', 'worktree_path', 'session_id',
                'gate_status', 'gate_result_json'):
        if key in kwargs:
            fields.append(f"{key} = ?")
            values.append(kwargs[key])

    values.append(task_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
            values
        )


def get_next_queued_task() -> Optional[dict]:
    """获取下一个排队中的任务"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_running_count() -> int:
    """获取当前运行中的任务数"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'running'"
        ).fetchone()
    return row['cnt']


# ────────────────────────────────
# Runtime CRUD
# ────────────────────────────────

def register_runtime(hostname: str, agent_type: str, binary_path: str, version: str = "") -> str:
    """注册或更新 agent 运行时"""
    runtime_id = f"{hostname}:{agent_type}"
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM runtimes WHERE id = ?", (runtime_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE runtimes SET binary_path=?, version=?, is_online=1,
                   last_heartbeat=datetime('now','localtime') WHERE id=?""",
                (binary_path, version, runtime_id)
            )
        else:
            conn.execute(
                """INSERT INTO runtimes (id, hostname, agent_type, binary_path, version,
                   last_heartbeat) VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))""",
                (runtime_id, hostname, agent_type, binary_path, version)
            )
    return runtime_id


def list_runtimes(online_only: bool = False) -> list[dict]:
    """列出所有注册的运行时"""
    with get_db() as conn:
        if online_only:
            rows = conn.execute("SELECT * FROM runtimes WHERE is_online = 1").fetchall()
        else:
            rows = conn.execute("SELECT * FROM runtimes ORDER BY agent_type").fetchall()
    return [dict(r) for r in rows]


def heartbeat_runtime(runtime_id: str):
    """更新运行时心跳"""
    with get_db() as conn:
        conn.execute(
            "UPDATE runtimes SET last_heartbeat = datetime('now','localtime'), is_online = 1 WHERE id = ?",
            (runtime_id,)
        )


def mark_runtimes_offline(timeout_minutes: int = 2):
    """标记超时未心跳的运行时为离线"""
    with get_db() as conn:
        conn.execute(
            """UPDATE runtimes SET is_online = 0
               WHERE last_heartbeat < datetime('now','localtime', ?)""",
            (f"-{timeout_minutes} minutes",)
        )


# ────────────────────────────────
# Stats
# ────────────────────────────────

def get_stats() -> dict:
    """获取仪表盘统计"""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM tasks").fetchone()['cnt']
        queued = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status='queued'").fetchone()['cnt']
        running = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status='running'").fetchone()['cnt']
        completed = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status='completed'").fetchone()['cnt']
        failed = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status='failed'").fetchone()['cnt']
        blocked = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status='blocked'").fetchone()['cnt']
        cancelled = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status='cancelled'").fetchone()['cnt']
        online_runtimes = conn.execute("SELECT COUNT(*) as cnt FROM runtimes WHERE is_online=1").fetchone()['cnt']
        agent_count = conn.execute("SELECT COUNT(*) as cnt FROM agents WHERE is_active=1").fetchone()['cnt']

    return {
        "total": total, "queued": queued, "running": running,
        "completed": completed, "failed": failed, "blocked": blocked,
        "cancelled": cancelled,
        "online_runtimes": online_runtimes, "agent_count": agent_count
    }


# ────────────────────────────────
# Agent CRUD
# ────────────────────────────────

def create_agent(name: str, system_prompt: str = "", model: str = "",
                 skills: list = None, tools: list = None) -> dict:
    """创建 Agent"""
    import json as _json
    agent_id = uuid.uuid4().hex[:8]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agents (id, name, system_prompt, model, skills, tools)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_id, name, system_prompt, model,
             _json.dumps(skills or [], ensure_ascii=False),
             _json.dumps(tools or [], ensure_ascii=False))
        )
    return get_agent(agent_id)


def get_agent(agent_id: str) -> Optional[dict]:
    """获取单个 Agent"""
    import json as _json
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["skills"] = _json.loads(d.get("skills", "[]"))
    d["tools"] = _json.loads(d.get("tools", "[]"))
    return d


def get_agent_by_name(name: str) -> Optional[dict]:
    """按名称获取 Agent（workflow 阶段 agent_ref 绑定用）"""
    import json as _json
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name = ? AND is_active = 1",
                           (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["skills"] = _json.loads(d.get("skills", "[]"))
    d["tools"] = _json.loads(d.get("tools", "[]"))
    return d


def list_agents(active_only: bool = False) -> list[dict]:
    """列出所有 Agent"""
    import json as _json
    with get_db() as conn:
        if active_only:
            rows = conn.execute("SELECT * FROM agents WHERE is_active = 1 ORDER BY name").fetchall()
        else:
            rows = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["skills"] = _json.loads(d.get("skills", "[]"))
        d["tools"] = _json.loads(d.get("tools", "[]"))
        result.append(d)
    return result


def update_agent(agent_id: str, **kwargs) -> dict:
    """更新 Agent 字段"""
    import json as _json
    allowed = {"name", "system_prompt", "model", "skills", "tools", "is_active"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k in ("skills", "tools") and isinstance(v, list):
            v = _json.dumps(v, ensure_ascii=False)
        fields.append(f"{k} = ?")
        values.append(v)
    if not fields:
        return get_agent(agent_id)
    fields.append("updated_at = datetime('now','localtime')")
    values.append(agent_id)
    with get_db() as conn:
        conn.execute(f"UPDATE agents SET {', '.join(fields)} WHERE id = ?", values)
    return get_agent(agent_id)


def delete_agent(agent_id: str):
    """删除 Agent（软删除）"""
    with get_db() as conn:
        conn.execute("UPDATE agents SET is_active = 0 WHERE id = ?", (agent_id,))


# ────────────────────────────────
# Comments CRUD
# ────────────────────────────────

def add_comment(task_id: str, content: str, author: str = "user") -> dict:
    """给任务添加评论"""
    cid = uuid.uuid4().hex[:8]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO comments (id, task_id, author, content) VALUES (?, ?, ?, ?)",
            (cid, task_id, author, content)
        )
    return get_comment(cid)


def get_comment(cid: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM comments WHERE id = ?", (cid,)).fetchone()
    return dict(row) if row else None


def list_comments(task_id: str) -> list[dict]:
    """列出任务的所有评论（按时间正序）"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ────────────────────────────────
# Task 增强操作
# ────────────────────────────────

def block_task(task_id: str, reason: str = ""):
    """阻塞任务"""
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET status='blocked', blocked_reason=?, updated_at=datetime('now','localtime') WHERE id=?",
            (reason, task_id)
        )


def unblock_task(task_id: str):
    """解除阻塞，回到 queued"""
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET status='queued', blocked_reason='', updated_at=datetime('now','localtime') WHERE id=? AND status='blocked'",
            (task_id,)
        )


# ────────────────────────────────
# 取消 & 僵尸任务恢复
# ────────────────────────────────

def request_cancel(task_id: str):
    """请求取消运行中的任务（daemon 轮询该标记并杀死对应进程）"""
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET cancel_requested=1, updated_at=datetime('now','localtime') WHERE id=?",
            (task_id,)
        )


def clear_cancel(task_id: str):
    """清除取消标记（claim/retry 时调用）"""
    with get_db() as conn:
        conn.execute("UPDATE tasks SET cancel_requested=0 WHERE id=?", (task_id,))


def list_cancel_requested() -> list:
    """列出所有请求了取消、且仍在 claimed/running 的任务 id"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE cancel_requested=1 AND status IN ('claimed','running')"
        ).fetchall()
    return [r['id'] for r in rows]


def requeue_stale_tasks(daemon_id: str = None) -> list:
    """
    僵尸任务恢复：daemon 启动时，把处于 claimed/running 的任务重新入队。
    daemon_id 不为空时只恢复该 daemon 认领的任务；None 时恢复全部（单 daemon 部署）。
    返回被恢复的任务 id 列表。
    """
    with get_db() as conn:
        if daemon_id:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('claimed','running') AND claimed_by=?",
                (daemon_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('claimed','running')"
            ).fetchall()
        ids = [r['id'] for r in rows]
        if ids:
            placeholders = ','.join('?' * len(ids))
            conn.execute(
                f"UPDATE tasks SET status='queued', claimed_by=NULL, cancel_requested=0, "
                f"updated_at=datetime('now','localtime') WHERE id IN ({placeholders})",
                ids
            )
    return ids
