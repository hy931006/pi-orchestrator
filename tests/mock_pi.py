#!/usr/bin/env python3
"""
Mock Pi CLI — 模拟 `pi -p --mode json` 的 JSON 事件流协议，用于测试。
通过 prompt 内容触发不同行为:
  含 "HANG"   → 永久挂起（测试超时）
  含 "SLOW=n" → 先输出再睡眠 n 秒（测试取消）
  含 "FAIL"   → 发出 error 事件但 exit 0（测试 error→failed）
  含 "RETRY"  → 发出 auto_retry_end success=false（测试重试耗尽→failed）
  含 "EXIT1"  → 写 stderr 并以退出码 1 退出
  其他        → 正常完成，输出含结构化工具标记与控制 token 的文本（测试清洗）
同时把收到的 argv 写入 PI_MOCK_ARGV_FILE 指向的文件，供测试断言参数构造。
"""
import json
import os
import re
import sys
import time


def emit(evt: dict):
    sys.stdout.write(json.dumps(evt, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]

    # 记录 argv 供测试断言
    argv_file = os.environ.get("PI_MOCK_ARGV_FILE")
    if argv_file:
        with open(argv_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv, ensure_ascii=False) + "\n")

    prompt = argv[-1] if argv else ""

    # session 文件：--session <path> 必须存在（模拟 pi 的行为），并向其追加事件
    if "--session" in argv:
        sp = argv[argv.index("--session") + 1]
        if not os.path.exists(sp):
            sys.stderr.write(f"session file not found: {sp}\n")
            sys.exit(1)
        with open(sp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_event", "prompt": prompt}, ensure_ascii=False) + "\n")

    # 读 stdin 直到 EOF（模拟 pi 等待父进程关闭 stdin，验证 #2188 修复）
    try:
        sys.stdin.read()
    except Exception:
        pass

    emit({"type": "agent_start"})

    if "HANG" in prompt:
        while True:
            time.sleep(1)

    emit({"type": "turn_start"})

    # thinking
    emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}})
    emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "先分析一下任务"}})
    emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_end"}})

    # 工具调用
    emit({"type": "tool_execution_start", "toolName": "bash", "toolCallId": "c1",
          "args": {"command": "echo hello"}})
    emit({"type": "tool_execution_end", "toolCallId": "c1", "result": "hello\n"})

    if "EXIT1" in prompt:
        sys.stderr.write("mock fatal error: something broke\n")
        sys.stderr.flush()
        sys.exit(1)

    if "FAIL" in prompt:
        emit({"type": "error", "message": "provider overloaded"})
        # 注意：以 0 退出 —— 测试 error 事件是否能把终态置为 failed
        sys.exit(0)

    if "RETRY" in prompt:
        emit({"type": "auto_retry_end", "success": False, "finalError": "rate limit exceeded"})
        sys.exit(0)

    # 文本输出：包含结构化工具标记 call:...{...} 和控制 token <|...|>，应被清洗
    for chunk in ["好的", "，任务", "已完成。",
                  "call:some_tool{\"a\": 1}",   # 结构化工具标记，应被剥离
                  "<|im_end>",                   # 控制 token，应被剥离
                  "最终答案：42"]:
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": chunk}})

    # turn_end 带 usage
    emit({"type": "turn_end", "message": {
        "role": "assistant", "model": "mock/mock-1",
        "usage": {"input": 100, "output": 50, "cacheRead": 10, "cacheWrite": 5}}})

    emit({"type": "agent_end"})

    if "SLOW" in prompt:
        m = re.search(r"SLOW=(\d+)", prompt)
        time.sleep(int(m.group(1)) if m else 10)

    sys.exit(0)


if __name__ == "__main__":
    main()
