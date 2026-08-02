#!/usr/bin/env python3
"""
agent.py 单元测试 — 用 mock pi 验证对 Multica pi.go 的像素级复刻。
运行: python tests/test_agent.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
from agent import (
    PiAgent, build_pi_args, split_pi_model, filter_custom_args,
    drain_pi_text_buffer, flush_pi_text_buffer, strip_pi_tool_call_markup,
    choose_pi_invocation,
)

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


def make_agent(tmpdir):
    """构造走 mock pi 的 PiAgent：python mock_pi.py <原始 pi 参数...>"""
    argv_file = os.path.join(tmpdir, "argv.jsonl")
    os.environ["PI_MOCK_ARGV_FILE"] = argv_file
    a = PiAgent(sys.executable)
    orig = agent.choose_pi_invocation
    a._test_orig = orig
    agent.choose_pi_invocation = lambda looked_up, args: (looked_up, [str(MOCK_PI)] + args)
    return a, argv_file


def restore_invocation():
    agent.choose_pi_invocation = agent.choose_pi_invocation.__wrapped__ if hasattr(agent.choose_pi_invocation, "__wrapped__") else _ORIG_INV


_ORIG_INV = agent.choose_pi_invocation


def read_argv(argv_file):
    with open(argv_file, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ────────────────────────────────
print("\n[1] 参数构造 build_pi_args / split_pi_model / filter_custom_args")
# ────────────────────────────────
args = build_pi_args("修复登录bug", "/tmp/s.jsonl", model="anthropic/claude-sonnet-4",
                     system_prompt="你是后端专家", custom_args=["--verbose"])
check("flag 顺序 -p --mode json", args[:3] == ["-p", "--mode", "json"])
check("--session", args[3:5] == ["--session", "/tmp/s.jsonl"])
check("provider/model 拆分", args[5:9] == ["--provider", "anthropic", "--model", "claude-sonnet-4"])
check("append-system-prompt", args[9:11] == ["--append-system-prompt", "你是后端专家"])
check("custom_args 在 prompt 前", args[11] == "--verbose")
check("prompt 最后", args[-1] == "修复登录bug")
check("split_pi_model 无斜杠", split_pi_model("gpt-5") == ("", "gpt-5"))
blocked = filter_custom_args(["-p", "x", "--mode", "xml", "--session", "/tmp/e", "--ok"])
check("blocked args 被过滤（standalone 不吞下一 token，与 Go 一致）", blocked == ["x", "--ok"], str(blocked))
blocked_inline = filter_custom_args(["--mode=xml", "'--verbose'", "--session=/tmp/e"])
check("inline= 形式也被过滤 + 引号剥离", blocked_inline == ["--verbose"], str(blocked_inline))

# ────────────────────────────────
print("\n[2] 文本清洗（复刻 drainPiTextBuffer）")
# ────────────────────────────────
buf = [""]
out = drain_pi_text_buffer(buf, "call:read{")
out += drain_pi_text_buffer(buf, '"path":"x"}')
out += flush_pi_text_buffer(buf)
check("结构化工具标记被剥离", out == "", repr(out))

out = strip_pi_tool_call_markup("hello<|im_end> world<tool_end|>")
# 注意：Go 正则 <|name>[A-Za-z0-9_-]* 会吞掉紧跟 token 的单词字符（像素级一致），
# 真实场景中 token 后是空白/换行，故用空格分隔测试
check("控制 token 被剥离", out == "hello world", repr(out))

buf = [""]
out = drain_pi_text_buffer(buf, "cal")          # "cal" 是 "call:" 前缀 → 应被 hold
check("不完整 call: 前缀被 hold", out == "", repr(out))
out += drain_pi_text_buffer(buf, "l:foo{}")
out += flush_pi_text_buffer(buf)
check("跨 chunk 的工具标记完整剥离", out == "", repr(out))

buf = [""]
out = drain_pi_text_buffer(buf, "普通文本")
out += flush_pi_text_buffer(buf)
check("普通文本原样输出", out == "普通文本", repr(out))

# ────────────────────────────────
print("\n[3] execute：正常完成 + 事件解析")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, argv_file = make_agent(tmp)
    r = a.execute(prompt="写个 hello world", cwd=tmp, model="anthropic/claude-sonnet-4",
                  system_prompt="你是专家")
    restore_invocation()
    check("status completed", r.status == "completed", r.error)
    check("success 属性", r.success)
    check("exit_code 0", r.exit_code == 0)
    check("文本已清洗", r.text == "好的，任务已完成。最终答案：42", repr(r.text))
    check("thinking 收集", "先分析一下任务" in r.thinking)
    check("tool_calls 记录", len(r.tool_calls) == 1 and r.tool_calls[0]["tool"] == "bash")
    check("tool_results 记录", len(r.tool_results) == 1 and r.tool_results[0]["output"] == "hello\n")
    check("usage 累计", r.usage.get("mock/mock-1", {}).get("input_tokens") == 100, str(r.usage))
    check("session_id 返回", r.session_id.endswith(".jsonl") and os.path.exists(r.session_id))
    check("duration_ms > 0", r.duration_ms > 0)

    calls = read_argv(argv_file)
    last = calls[-1]
    check("mock 收到 --session", "--session" in last)
    check("mock 收到 --provider/--model",
          "--provider" in last and last[last.index("--provider") + 1] == "anthropic")
    check("mock 收到 --append-system-prompt",
          "--append-system-prompt" in last and last[last.index("--append-system-prompt") + 1] == "你是专家")
    # session 文件被 mock 追加了事件（证明 --session 路径真实生效）
    with open(r.session_id, encoding="utf-8") as f:
        check("session 文件被写入", "session_event" in f.read())

# ────────────────────────────────
print("\n[4] execute：error 事件 → failed（即使 exit 0）")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, _ = make_agent(tmp)
    r = a.execute(prompt="FAIL 这个任务", cwd=tmp)
    restore_invocation()
    check("status failed", r.status == "failed", r.status)
    check("error 内容", r.error == "provider overloaded", r.error)
    check("errors 列表", "provider overloaded" in r.errors)
    check("success=False", not r.success)

# ────────────────────────────────
print("\n[5] execute：auto_retry_end 失败 → failed")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, _ = make_agent(tmp)
    r = a.execute(prompt="RETRY 场景", cwd=tmp)
    restore_invocation()
    check("status failed", r.status == "failed", r.status)
    check("finalError", r.error == "rate limit exceeded", r.error)

# ────────────────────────────────
print("\n[6] execute：非零退出码 + stderr tail")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, _ = make_agent(tmp)
    r = a.execute(prompt="EXIT1 场景", cwd=tmp)
    restore_invocation()
    check("status failed", r.status == "failed", r.status)
    check("exit_code 1", r.exit_code == 1, str(r.exit_code))
    check("stderr 进入 error", "mock fatal error" in r.error, r.error)

# ────────────────────────────────
print("\n[7] execute：超时 → timeout（看门狗杀进程）")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, _ = make_agent(tmp)
    t0 = time.monotonic()
    r = a.execute(prompt="HANG 住", cwd=tmp, timeout=3)
    elapsed = time.monotonic() - t0
    restore_invocation()
    check("status timeout", r.status == "timeout", r.status)
    check("error 含超时信息", "timed out" in r.error, r.error)
    check("在看门狗时限内返回", elapsed < 10, f"{elapsed:.1f}s")

# ────────────────────────────────
print("\n[8] execute：cancel_event → aborted")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, _ = make_agent(tmp)
    ev = threading.Event()
    threading.Timer(2.0, ev.set).start()
    t0 = time.monotonic()
    r = a.execute(prompt="SLOW=60 长任务", cwd=tmp, cancel_event=ev, timeout=120)
    elapsed = time.monotonic() - t0
    restore_invocation()
    check("status aborted", r.status == "aborted", r.status)
    check("error 内容", r.error == "execution cancelled", r.error)
    check("及时返回", elapsed < 15, f"{elapsed:.1f}s")

# ────────────────────────────────
print("\n[9] execute：session resume（ResumeSessionID）")
# ────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    a, _ = make_agent(tmp)
    r1 = a.execute(prompt="第一轮", cwd=tmp)
    r2 = a.execute(prompt="第二轮", cwd=tmp, resume_session_id=r1.session_id)
    restore_invocation()
    check("resume 复用同一路径", r2.session_id == r1.session_id)
    with open(r1.session_id, encoding="utf-8") as f:
        content = f.read()
    check("session 文件含两轮事件", "第一轮" in content and "第二轮" in content)

# ────────────────────────────────
print("\n[10] choose_pi_invocation（Windows pi.cmd → powershell -File）")
# ────────────────────────────────
if agent.IS_WINDOWS:
    with tempfile.TemporaryDirectory() as tmp:
        cmd_path = Path(tmp) / "pi.cmd"
        ps1_path = Path(tmp) / "pi.ps1"
        cmd_path.write_text("@echo off\n")
        ps1_path.write_text("# mock\n")
        argv0, full = choose_pi_invocation(str(cmd_path), ["-p", "--mode", "json", "hi"])
        check("重写为 powershell", "powershell" in argv0.lower() or "pwsh" in argv0.lower(), argv0)
        check("-File 指向 ps1", "-File" in full and full[full.index("-File") + 1] == str(ps1_path))
        check("参数保留", full[-4:] == ["-p", "--mode", "json", "hi"])
        # 无 ps1 → 原样返回
        ps1_path.unlink()
        argv0, full = choose_pi_invocation(str(cmd_path), ["-p"])
        check("无 ps1 回退原样", argv0 == str(cmd_path) and full == ["-p"])
else:
    argv0, full = choose_pi_invocation("/usr/bin/pi", ["-p"])
    check("非 Windows 原样传递", argv0 == "/usr/bin/pi" and full == ["-p"])

print(f"\n{'='*50}\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
