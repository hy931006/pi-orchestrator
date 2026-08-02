"""backends — Coding Agent 执行层抽象

目标：将「执行层」与「编排层」解耦。编排层（daemon/workflow）只依赖
AgentBackend 抽象接口，不关心具体是 pi / omp / claude-code / codex / 其他。

替换 backend 的方式:
  1. 实现 AgentBackend 子类（execute 契约）
  2. 用 @register_backend("name") 注册
  3. config.yaml 或环境变量 PI_ORCHESTRATOR_BACKEND 选择
"""
import threading

class AgentResult:
    """执行结果通用契约（跨 backend 统一字段）

    终态 status: completed / failed / timeout / aborted
    """

    def __init__(self):
        self.text: str = ""            # 纯文本输出（清洗后）
        self.thinking: str = ""        # 思考过程
        self.tool_calls: list = []     # 工具调用记录 [{tool, call_id, input}]
        self.tool_results: list = []   # 工具调用结果 [{call_id, output, is_error}]
        self.errors: list = []         # 错误消息列表
        self.status: str = "pending"   # completed / failed / timeout / aborted
        self.exit_code: int = -1
        self.error: str = ""
        self.session_id: str = ""      # 会话标识（可 resume）
        self.duration_ms: int = 0
        self.usage: dict = {}          # model → {input/output/cache_read/cache_write tokens}

    @property
    def success(self) -> bool:
        return self.status == "completed"


class AgentBackend:
    """Coding Agent 执行后端抽象基类

    子类需实现 execute()。其余方法可选覆写。
    """

    #: 后端名称（注册键，如 "pi" / "claude-code" / "codex"）
    name: str = "base"

    def __init__(self, timeout: int = 7200):
        self.timeout = timeout

    def execute(self, prompt: str, cwd: str = None, model: str = "",
                system_prompt: str = "", custom_args: list = None,
                resume_session_id: str = "", timeout: int = None,
                cancel_event: threading.Event = None,
                on_event=None) -> AgentResult:
        """执行一次任务。

        Args:
            prompt: 任务指令（位置语义由后端决定）
            cwd: 工作目录
            model: 模型标识（provider/model 或后端原生格式）
            system_prompt: 系统人格注入
            custom_args: 透传给 CLI 的额外参数
            resume_session_id: 续聊上一会话
            timeout: 覆盖默认超时（秒）
            cancel_event: 外部置位即中止（aborted）
            on_event: 可选回调，流式事件 (dict) 实时推送

        Returns:
            AgentResult（status 语义统一）
        """
        raise NotImplementedError

    def cancel_all(self):
        """中止所有活跃进程（关停时调用）"""


# ═══════════════════════════════════════════
# 注册表 / 工厂
# ═══════════════════════════════════════════

_REGISTRY: dict = {}


def register_backend(name: str):
    """类装饰器：注册 backend 到注册表"""
    def decorator(cls):
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return decorator


def list_backends() -> list:
    """列出已注册的 backend 名称"""
    return sorted(_REGISTRY)


def create_backend(name: str = None, **kwargs) -> AgentBackend:
    """工厂：按名称实例化 backend。

    name 为空时从配置/环境读取（优先 PI_ORCHESTRATOR_BACKEND，默认 pi）。
    kwargs 透传给后端构造器（如 pi_path、timeout）。
    """
    if not name:
        import os
        name = os.environ.get("PI_ORCHESTRATOR_BACKEND", "pi")
    cls = _REGISTRY.get(name)
    if not cls:
        raise ValueError(f"未知 backend: {name}，可用: {list_backends()}")
    return cls(**kwargs)
