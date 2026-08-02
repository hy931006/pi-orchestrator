#!/usr/bin/env python3
"""test_backends.py — 执行层抽象单元测试"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backends import (  # noqa: E402
    AgentBackend, AgentResult, create_backend, list_backends, register_backend,
)
from agent import PiAgent, PiResult, detect_pi  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def test_all():
    # 1. 注册表包含内置 backend
    names = list_backends()
    check("注册表含 pi+echo", "pi" in names and "echo" in names, str(names))

    # 2. AgentResult 契约字段
    r = AgentResult()
    for f in ("text", "thinking", "tool_calls", "tool_results", "errors",
              "status", "exit_code", "error", "session_id", "duration_ms", "usage"):
        check(f"AgentResult.{f} 存在", hasattr(r, f))
    check("success=False (pending)", r.success is False)
    r.status = "completed"
    check("success=True (completed)", r.success is True)

    # 3. PiResult 继承 AgentResult
    pr = PiResult()
    check("PiResult 是 AgentResult 子类", isinstance(pr, AgentResult))
    check("PiResult 字段兼容", pr.text == "" and pr.usage == {})

    # 4. PiAgent 是 AgentBackend 且注册名 pi
    check("PiAgent 是 AgentBackend 子类", issubclass(PiAgent, AgentBackend))
    check("PiAgent.name == 'pi'", PiAgent.name == "pi")

    # 5. 工厂创建 echo
    echo = create_backend("echo", delay=0)
    check("工厂创建 echo", isinstance(echo, AgentBackend))
    res = echo.execute("测试任务", model="test/m")
    check("echo 执行 completed", res.status == "completed", res.status)
    check("echo 回显 prompt", "测试任务" in res.text)
    check("echo usage 存在", "echo" in res.usage)

    # 6. 工厂创建 pi（需 pi 存在，否则跳过）
    pi_path = detect_pi()
    if pi_path:
        pi = create_backend("pi", pi_path=pi_path, timeout=60)
        check("工厂创建 pi", isinstance(pi, PiAgent))
        check("pi timeout 透传", pi.timeout == 60)
    else:
        print("  ⏭️  pi 未安装，跳过 pi 工厂测试")

    # 7. 未知 backend 报错
    try:
        create_backend("nope")
        check("未知 backend 报错", False)
    except ValueError:
        check("未知 backend 报错", True)

    # 8. 默认工厂 = pi（环境变量未设时）
    import os
    os.environ.pop("PI_ORCHESTRATOR_BACKEND", None)
    if pi_path:
        b = create_backend()
        check("默认 backend=pi", b.name == "pi", b.name)
    else:
        print("  ⏭️  pi 未安装，跳过默认 backend 测试")

    # 9. 环境变量切换 backend
    os.environ["PI_ORCHESTRATOR_BACKEND"] = "echo"
    b2 = create_backend()
    check("环境变量切换 echo", b2.name == "echo", b2.name)
    os.environ.pop("PI_ORCHESTRATOR_BACKEND", None)

    # 10. 自定义 backend 注册
    @register_backend("my-test")
    class MyBackend(AgentBackend):
        def execute(self, prompt, **kw):
            r = AgentResult()
            r.status = "completed"
            r.text = "my"
            return r

    check("自定义注册生效", "my-test" in list_backends())
    res2 = create_backend("my-test").execute("x")
    check("自定义 backend 可执行", res2.text == "my")

    print(f"\n{'='*50}\nBackends 测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    test_all()
