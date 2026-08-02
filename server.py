"""
Pi Orchestrator — FastAPI 服务器（纯 API + Web UI，无内嵌 daemon）
Daemon 作为独立进程运行：python daemon.py
"""
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

import database as db
import workflow as wf

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [server] %(levelname)s: %(message)s")
logger = logging.getLogger("server")

db.init_db()
TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Pi Orchestrator")


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    agent_type: str = "auto"
    repo_path: str = ""


# ── Web UI ──

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(TEMPLATES_DIR / "index.html")


# ── Tasks ──

@app.get("/api/tasks")
async def list_tasks(status: str = Query(None), limit: int = 50, offset: int = 0):
    tasks = db.list_tasks(status=status, limit=limit, offset=offset)
    return {"tasks": tasks, "stats": db.get_stats()}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    task = db.create_task(
        title=req.title, description=req.description,
        agent_type=req.agent_type,
        repo_path=req.repo_path or str(Path.cwd())
    )
    logger.info(f"📝 [{task['id']}] {task['title']}")
    return task


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404)
    if task["status"] in ("running", "claimed"):
        raise HTTPException(status_code=400, detail="Cannot delete running task")
    with db.get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"deleted": task_id}


@app.put("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404)
    if task["status"] not in ("failed", "completed", "cancelled"):
        raise HTTPException(status_code=400)
    db.clear_cancel(task_id)
    db.update_task_status(task_id, "queued")
    db.add_comment(task_id, "🔁 Retry — back to queue", "system")
    return db.get_task(task_id)


@app.put("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务：queued → 直接取消；claimed/running → 置取消标记，daemon 杀进程"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404)
    status = task["status"]
    if status == "queued":
        db.update_task_status(task_id, "cancelled", result="cancelled before start")
        db.add_comment(task_id, "🛑 Cancelled before start", "system")
    elif status in ("claimed", "running"):
        db.request_cancel(task_id)
        db.add_comment(task_id, "🛑 Cancel requested", "system")
    else:
        raise HTTPException(status_code=400, detail=f"Cannot cancel task in status: {status}")
    return db.get_task(task_id)


# ── Agents ──

@app.get("/api/agents")
async def list_agents():
    return db.list_agents()


@app.post("/api/agents")
async def create_agent(req: dict):
    agent = db.create_agent(
        name=req.get("name", ""),
        system_prompt=req.get("system_prompt", ""),
        model=req.get("model", ""),
        skills=req.get("skills", []),
        tools=req.get("tools", [])
    )
    logger.info(f"🤖 Agent created: {agent['name']}")
    return agent


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = db.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404)
    return agent


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, req: dict):
    agent = db.update_agent(agent_id, **req)
    if not agent:
        raise HTTPException(status_code=404)
    return agent


@app.delete("/api/agents/{agent_id}")
async def api_delete_agent(agent_id: str):
    db.delete_agent(agent_id)
    return {"deleted": agent_id}


# ── Comments ──

@app.get("/api/tasks/{task_id}/comments")
async def list_comments(task_id: str):
    return db.list_comments(task_id)


@app.post("/api/tasks/{task_id}/comments")
async def add_comment(task_id: str, req: dict):
    c = db.add_comment(task_id, req.get("content", ""), req.get("author", "user"))
    logger.info(f"💬 Comment on [{task_id}]: {c['content'][:60]}")
    return c


# ── Task lifecycle ──

@app.put("/api/tasks/{task_id}/block")
async def block_task(task_id: str, req: dict = None):
    reason = (req or {}).get("reason", "")
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404)
    # 阻塞一个正在执行的任务时，同时请求取消其进程
    if task["status"] in ("claimed", "running"):
        db.request_cancel(task_id)
    db.block_task(task_id, reason)
    db.add_comment(task_id, f"🚫 Blocked: {reason}" if reason else "🚫 Blocked", "system")
    return db.get_task(task_id)


@app.put("/api/tasks/{task_id}/unblock")
async def api_unblock_task(task_id: str):
    db.unblock_task(task_id)
    db.add_comment(task_id, "✅ Unblocked — back to queue", "system")
    return db.get_task(task_id)


# ── Runtimes ──

@app.get("/api/runtimes")
async def list_runtimes():
    import shutil
    agents = []
    for name, binary in [("pi", "pi"), ("omp", "omp")]:
        path = shutil.which(binary)
        if path:
            agents.append({"name": name, "binary": binary, "path": path})
    return {"local_agents": agents, "registered_runtimes": db.list_runtimes()}


# ── Stats ──

@app.get("/api/stats")
async def get_stats():
    return db.get_stats()


# ── SSE ──

@app.get("/api/stream")
async def stream(request: Request):
    import asyncio
    from fastapi.responses import StreamingResponse

    async def events():
        while True:
            if await request.is_disconnected():
                break
            try:
                tasks = db.list_tasks(limit=20)
                stats = db.get_stats()
                yield f"data: {json.dumps({'tasks': tasks, 'stats': stats}, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            await asyncio.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


# ── Health ──

@app.get("/health")
async def health():
    import shutil
    agents = sum(1 for b in ["pi", "omp"] if shutil.which(b))
    return {"status": "ok", "agents": agents}


# ── Workflows ──

class CreateWorkflowRequest(BaseModel):
    template_name: str
    title: str
    repo_path: str = ""


def _current_stage_task_id(run_id: str, stage_index: int):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE workflow_run_id=? AND stage_index=?",
            (run_id, stage_index)).fetchone()
    return row["id"] if row else None


def _audit(task_id: str, action: str, stage: str, reason: str = ""):
    """审计评论（设计 §14.1）"""
    reason_part = f" reason={reason}" if reason else ""
    db.add_comment(task_id,
                   f"[AUDIT] 阶段流转 action={action} reviewer=user stage={stage}{reason_part}",
                   "system")


@app.post("/api/workflows")
async def create_workflow(req: CreateWorkflowRequest):
    try:
        result = wf.create_workflow(req.template_name, req.title, req.repo_path or None)
    except wf.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.get("/api/workflows")
async def list_workflows(status: str = Query(None)):
    return {"workflows": db.list_workflow_runs(status=status)}


@app.get("/api/workflows/templates")
async def list_templates():
    return {"templates": list(wf.load_templates().values())}


@app.get("/api/workflows/{run_id}")
async def get_workflow(run_id: str):
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    return run


@app.get("/api/workflows/{run_id}/stages")
async def get_workflow_stages(run_id: str):
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    with db.get_db() as conn:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE workflow_run_id=? ORDER BY stage_index",
            (run_id,)).fetchall()
    return {"stages": [dict(t) for t in tasks]}


@app.get("/api/workflows/{run_id}/stage/{stage_key}")
async def get_stage(run_id: str, stage_key: str):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE workflow_run_id=? AND stage_key=?",
            (run_id, stage_key)).fetchone()
    if not row:
        raise HTTPException(status_code=404)
    return dict(row)


@app.post("/api/workflows/{run_id}/approve")
async def approve_stage(run_id: str):
    """批准当前阶段 → 流转下一阶段"""
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "completed", gate_status="approved")
        _audit(task_id, "approve", run["current_stage"] or "")
    try:
        wf.advance_stage(run_id)
    except wf.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return db.get_workflow_run(run_id)


@app.post("/api/workflows/{run_id}/reject")
async def reject_stage(run_id: str, reason: str = Query("")):
    """驳回 → 当前阶段回到 queued（resume 重做）"""
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "queued", gate_status="rejected")
        _audit(task_id, "reject", run["current_stage"] or "", reason)
    return db.get_workflow_run(run_id)


@app.post("/api/workflows/{run_id}/force")
async def force_stage(run_id: str, reason: str = Query("")):
    """强制流转（管理员语义，reason 必填）"""
    if not reason:
        raise HTTPException(status_code=400, detail="force 操作 reason 必填")
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "completed", gate_status="forced")
        _audit(task_id, "force", run["current_stage"] or "", reason)
    try:
        wf.advance_stage(run_id)
    except wf.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return db.get_workflow_run(run_id)


@app.post("/api/workflows/{run_id}/waive")
async def waive_stage(run_id: str, reason: str = Query("")):
    """豁免当前阶段（跳过流转，reason 必填）"""
    if not reason:
        raise HTTPException(status_code=400, detail="waive 操作 reason 必填")
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=404)
    task_id = _current_stage_task_id(run_id, run["current_stage_index"])
    if task_id:
        db.update_task_status(task_id, "completed", gate_status="waived")
        _audit(task_id, "waive", run["current_stage"] or "", reason)
    try:
        wf.advance_stage(run_id)
    except wf.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return db.get_workflow_run(run_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020, log_level="info")
