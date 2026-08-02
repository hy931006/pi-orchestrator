"""backends 包 — Coding Agent 执行层抽象

内置 backend:
  - pi   : Pi Coding Agent（定义在 agent.py，@register_backend("pi")）
  - echo : 回显后端（演示/测试可插拔性）

替换 backend: 实现 AgentBackend 子类 → @register_backend("name") →
设置 PI_ORCHESTRATOR_BACKEND=name 或改 config.yaml。
"""
from backends.base import (
    AgentBackend,
    AgentResult,
    create_backend,
    list_backends,
    register_backend,
)

# 触发注册：import agent（PiAgent 带 @register_backend("pi")）与 echo
import agent  # noqa: F401  （agent.py 内 PiAgent 注册 "pi"）
import backends.echo  # noqa: F401

__all__ = [
    "AgentBackend",
    "AgentResult",
    "create_backend",
    "list_backends",
    "register_backend",
]
