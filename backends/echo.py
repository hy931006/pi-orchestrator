"""backends.echo — 演示/测试用 backend

不做真实 LLM 调用：把 prompt 原样回显为结果文本。
用于验证抽象层可插拔性（编排层无需改动即可切换 backend）。

用法:
    PI_ORCHESTRATOR_BACKEND=echo python daemon.py
"""
import time

from backends.base import AgentBackend, AgentResult, register_backend


@register_backend("echo")
class EchoBackend(AgentBackend):
    """Echo backend：回显 prompt，模拟一次完整执行（含 thinking/tool/usage）"""

    name = "echo"

    def __init__(self, timeout: int = 7200, delay: float = 0.0):
        super().__init__(timeout=timeout)
        self.delay = delay

    def execute(self, prompt: str, cwd: str = None, model: str = "",
                system_prompt: str = "", custom_args: list = None,
                resume_session_id: str = "", timeout: int = None,
                cancel_event=None, on_event=None) -> AgentResult:
        t0 = time.monotonic()
        if self.delay:
            time.sleep(self.delay)

        r = AgentResult()
        r.status = "completed"
        r.text = f"[echo] 收到任务（backend=echo, model={model or '默认'}）:\n{prompt}"
        r.thinking = f"（echo backend 无真实思考）model={model}"
        r.tool_calls = []
        r.session_id = resume_session_id or f"echo-{int(t0)}"
        r.duration_ms = int((time.monotonic() - t0) * 1000)
        r.usage = {"echo": {"input_tokens": len(prompt), "output_tokens": len(r.text),
                            "cache_read_tokens": 0, "cache_write_tokens": 0}}
        return r
