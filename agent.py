"""
Pi Agent 适配器 — 像素级复刻 Multica server/pkg/agent/pi.go

对应关系:
  piBackend.Execute()          → PiAgent.execute()
  buildPiArgs()                → build_pi_args()
  splitPiModel()               → split_pi_model()
  choosePiInvocation()         → choose_pi_invocation()   (Windows: pi.cmd → powershell -File pi.ps1, #3306)
  newPiSessionPath()           → new_pi_session_path()
  ensurePiSessionFile()        → ensure_pi_session_file()
  drainPiTextBuffer 等文本清洗  → drain_pi_text_buffer / flush_pi_text_buffer 等

核心行为（与 Go 版一致）:
1. pi -p --mode json --session <path> [...] <prompt>，prompt 为最后一个位置参数
2. 显式 stdin pipe，Start 后立即关闭发送 EOF（Multica #2188）
3. stderr 独立管道（不混入 stdout 的 JSON 流）
4. 逐行解析 stdout JSON 事件：agent_start / turn_start / message_update /
   tool_execution_start|end / turn_end / error / auto_retry_end
5. text_delta 经结构化工具标记 + 控制 token 清洗后输出
6. 超时 / 取消通过杀死进程实现（Windows 下杀进程树）
7. error 事件 / auto_retry_end 失败 / 非零退出码 → failed
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent")

IS_WINDOWS = sys.platform == "win32"


def strip_ansi(text: str) -> str:
    """清洗 ANSI 转义序列（CSI + 私有参数）"""
    text = re.sub(r'\x1b\[[0-9;?<=>]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b[()][0-9]', '', text)
    return text


def detect_pi() -> Optional[str]:
    """检测 pi 可执行文件。
    优先 PI_EXECUTABLE 环境变量（对应 Multica 的 cfg.ExecutablePath），
    否则在 PATH 上查找；Windows 下 PATH 缺失时回退 %APPDATA%\\npm。"""
    override = os.environ.get("PI_EXECUTABLE")
    if override and Path(override).exists():
        logger.info(f"✅ Pi detected (PI_EXECUTABLE): {override}")
        return override
    path = shutil.which("pi")
    if path:
        logger.info(f"✅ Pi detected: {path}")
        return path
    if IS_WINDOWS:
        npm_dir = os.environ.get("APPDATA")
        if npm_dir:
            for name in ("pi.cmd", "pi.ps1", "pi.bat"):
                candidate = Path(npm_dir) / "npm" / name
                if candidate.exists():
                    logger.info(f"✅ Pi detected (npm global fallback): {candidate}")
                    return str(candidate)
    return None


# ═══════════════════════════════════════════
# 文本清洗 — 复刻 pi.go 的 markup/control-token stripping
# ═══════════════════════════════════════════

# <|token>name / <name|> 形式的控制 token
_PI_CONTROL_TOKEN_RE = re.compile(r'<\|[A-Za-z0-9_-]+>[A-Za-z0-9_-]*|<[A-Za-z0-9_-]+\|>')


def strip_pi_tool_call_markup(s: str) -> str:
    s = strip_pi_structured_tool_markup(s)
    return _PI_CONTROL_TOKEN_RE.sub('', s)


def drain_pi_text_buffer(buf: list, delta: str) -> str:
    """buf 是单元素 list，模拟 Go 的 strings.Builder 指针语义"""
    buf[0] += delta
    emit, pending = drain_pi_sanitized_text(buf[0])
    buf[0] = pending
    return emit


def flush_pi_text_buffer(buf: list) -> str:
    s = buf[0]
    buf[0] = ""
    emit, pending = drain_pi_sanitized_text(s)
    emit += _PI_CONTROL_TOKEN_RE.sub('', pending)
    return emit


def drain_pi_sanitized_text(s: str) -> tuple:
    """返回 (可安全发出的文本, 需保留等待更多数据的尾部)"""
    out = []
    i = 0
    n = len(s)
    while i < n:
        start, prefix_len = next_pi_tool_markup_prefix(s, i)
        if start == -1:
            safe_len = safe_pi_text_emit_len(s[i:])
            out.append(s[i:i + safe_len])
            return _PI_CONTROL_TOKEN_RE.sub('', ''.join(out)), s[i + safe_len:]
        out.append(s[i:start])
        end, ok = scan_pi_tool_markup_end(s, start + prefix_len)
        if not ok:
            return _PI_CONTROL_TOKEN_RE.sub('', ''.join(out)), s[start:]
        i = end
    return _PI_CONTROL_TOKEN_RE.sub('', ''.join(out)), ""


def strip_pi_structured_tool_markup(s: str) -> str:
    out = []
    i = 0
    n = len(s)
    while i < n:
        start, prefix_len = next_pi_tool_markup_prefix(s, i)
        if start == -1:
            out.append(s[i:])
            break
        out.append(s[i:start])
        end, ok = scan_pi_tool_markup_end(s, start + prefix_len)
        if not ok:
            out.append(s[start:])
            break
        i = end
    return ''.join(out)


def safe_pi_text_emit_len(s: str) -> int:
    hold = 0
    for prefix in ("call:", "response:"):
        # Go: for n := 1; n < len(prefix) && n <= len(s); n++
        for n in range(1, min(len(prefix) - 1, len(s)) + 1):
            if s.endswith(prefix[:n]) and n > hold:
                hold = n
    i = s.rfind('<')
    if i >= 0 and looks_like_pi_control_token_prefix(s[i:]):
        if len(s) - i > hold:
            hold = len(s) - i
    return len(s) - hold


def looks_like_pi_control_token_prefix(s: str) -> bool:
    if not s or s[0] != '<' or len(s) > 64:
        return False
    for b in s[1:]:
        if not (b.isalnum() and b.isascii()) and b not in '_-|>':
            return False
    return True


def next_pi_tool_markup_prefix(s: str, from_: int) -> tuple:
    best = -1
    best_len = 0
    for prefix in ("call:", "response:"):
        i = s.find(prefix, from_)
        if i >= 0 and (best == -1 or i < best):
            best = i
            best_len = len(prefix)
    return best, best_len


def scan_pi_tool_markup_end(s: str, i: int) -> tuple:
    name_start = i
    n = len(s)
    while i < n and _is_pi_tool_name_byte(s[i]):
        i += 1
    if i == name_start or i >= n or s[i] != '{':
        return 0, False

    QUOTE_MARKER = '<|"|>'
    depth = 0
    in_quote = False
    while i < n:
        if s.startswith(QUOTE_MARKER, i):
            in_quote = not in_quote
            i += len(QUOTE_MARKER)
            continue
        if not in_quote:
            c = s[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    i += 1
                    if s.startswith('<tool_call|>', i):
                        i += len('<tool_call|>')
                    return i, True
        i += 1
    return 0, False


def _is_pi_tool_name_byte(ch: str) -> bool:
    return (ch.isascii() and ch.isalnum()) or ch in '_-'


# ═══════════════════════════════════════════
# Session 路径 — 复刻 newPiSessionPath / ensurePiSessionFile
# ═══════════════════════════════════════════

def pi_session_dir() -> Path:
    return Path.home() / ".pi-orchestrator" / "pi-sessions"


def new_pi_session_path() -> str:
    # Go: time.Now().UTC().Format("20060102T150405.000000000")
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + "000.jsonl"
    return str(pi_session_dir() / name)


def ensure_pi_session_file(path: str):
    """Pi 拒绝在 --session 指向不存在的文件时启动；已存在的 resume 路径不动"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)


# ═══════════════════════════════════════════
# 参数构造 — 复刻 buildPiArgs / splitPiModel
# ═══════════════════════════════════════════

# daemon 硬编码、不允许 custom_args 覆盖的 flags（复刻 piBlockedArgs）
# 值: "standalone"=无值 flag / "with_value"=flag+值 / "optional_value"=值可选
PI_BLOCKED_ARGS = {
    "-p": "standalone",       # 非交互模式
    "--print": "standalone",  # -p 的别名
    "--mode": "with_value",   # "json" 事件流协议
    "--session": "with_value"  # session 路径由 daemon 管理
}


def _strip_surrounding_quotes(s: str):
    if len(s) >= 2 and ((s[0] == "'" and s[-1] == "'") or (s[0] == '"' and s[-1] == '"')):
        return s[1:-1], True
    return s, False


def split_pi_model(s: str) -> tuple:
    """"provider/model" → (provider, model)；无斜杠 → ("", model)"""
    s = s.strip()
    if '/' in s:
        provider, model = s.split('/', 1)
        return provider.strip(), model.strip()
    return "", s


def _unshell_quote_arg(arg: str) -> str:
    """复刻 unshellQuoteArg：仅 flag 形式的内联值和整体成对引号被剥离"""
    if arg.startswith("-"):
        idx = arg.find("=")
        if idx > 0:
            unquoted, ok = _strip_surrounding_quotes(arg[idx + 1:])
            return arg[:idx + 1] + unquoted if ok else arg
    unquoted, ok = _strip_surrounding_quotes(arg)
    return unquoted if ok else arg


def filter_custom_args(custom_args: list) -> list:
    """过滤会破坏 daemon↔Pi 协议的 flags（像素级复刻 filterCustomArgs + piBlockedArgs）"""
    if not custom_args:
        return []
    filtered = []
    i = 0
    while i < len(custom_args):
        arg = _unshell_quote_arg(custom_args[i])
        flag = arg
        has_inline_value = False
        idx = arg.find("=")
        if idx > 0:
            flag = arg[:idx]
            has_inline_value = True
        mode = PI_BLOCKED_ARGS.get(flag)
        if mode:
            logger.warning(f"custom_args: 忽略协议关键 flag: {flag}")
            if mode == "with_value" and not has_inline_value:
                i += 1  # 下一个 token 是该 flag 的值，一并跳过
            elif mode == "optional_value" and not has_inline_value and \
                    i + 1 < len(custom_args) and \
                    not _unshell_quote_arg(custom_args[i + 1]).startswith("-"):
                i += 1
            i += 1
            continue
        filtered.append(arg)
        i += 1
    return filtered


def build_pi_args(prompt: str, session_path: str, model: str = "",
                  system_prompt: str = "", custom_args: list = None) -> list:
    """
    复刻 buildPiArgs:
      -p --mode json [--session p] [--provider x --model y]
      [--append-system-prompt s] [custom_args...] <prompt>
    prompt 是位置参数，必须最后。
    """
    args = ["-p", "--mode", "json"]
    if session_path:
        args += ["--session", session_path]
    if model:
        provider, model_id = split_pi_model(model)
        if provider:
            args += ["--provider", provider]
        if model_id:
            args += ["--model", model_id]
    # 不传 --tools：省略让 Pi 用完整工具注册表（含扩展工具），--tools 是限制性白名单 (#2379)
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    args += filter_custom_args(custom_args)
    args.append(prompt)
    return args


# ═══════════════════════════════════════════
# 调用方式选择 — 复刻 choosePiInvocation (Windows #3306)
# ═══════════════════════════════════════════

def _powershell_lookup() -> Optional[str]:
    """优先 pwsh.exe (PowerShell 7)，回退系统 powershell.exe"""
    for name in ("pwsh.exe", "powershell.exe"):
        p = shutil.which(name)
        if p:
            return p
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    p = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.exists() else None


def choose_pi_invocation(looked_up: str, args: list) -> tuple:
    """
    返回 (argv0, full_args)。
    Windows 上 npm 的 pi binstub 是 pi.cmd，内容是 powershell -File pi.ps1 %*。
    .cmd 经 cmd.exe 执行时 %* 会重新分词，破坏含换行的多行 prompt (#3306)。
    复刻 rewriteCmdToPS1：直接用 PowerShell -File pi.ps1 启动，argv 逐 token 传递。
    """
    if IS_WINDOWS and looked_up.lower().endswith((".cmd", ".bat")):
        tool_name = Path(looked_up).stem  # "pi"
        ps1 = Path(looked_up).parent / f"{tool_name}.ps1"
        if ps1.exists() and not ps1.is_dir():
            ps_exe = _powershell_lookup()
            if ps_exe:
                full = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)] + args
                logger.info(f"pi: 经 powershell -File 启动以保留 argv token: {ps1}")
                return ps_exe, full
    return looked_up, args


def kill_process_tree(process: subprocess.Popen):
    """超时/取消时杀进程。Windows 下 pi 经 powershell→node 派生，需杀整棵树。"""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            process.kill()
    except Exception as e:
        logger.warning(f"kill process failed: {e}")
        try:
            process.kill()
        except Exception:
            pass


# ═══════════════════════════════════════════
# PiAgent
# ═══════════════════════════════════════════

class PiResult:
    """Pi 执行结果（对应 Go 的 Result + 流式累积状态）"""

    def __init__(self):
        self.text: str = ""            # 纯文本输出（清洗后）
        self.thinking: str = ""        # 思考过程
        self.tool_calls: list = []     # 工具调用记录
        self.tool_results: list = []   # 工具调用结果
        self.errors: list = []         # error 事件
        # 终态: completed / failed / timeout / aborted（复刻 Go 的 finalStatus）
        self.status: str = "pending"
        self.exit_code: int = -1
        self.error: str = ""
        self.session_id: str = ""      # session 文件路径，可作为 ResumeSessionID 复用
        self.duration_ms: int = 0
        self.usage: dict = {}          # model → {input/output/cache_read/cache_write tokens}

    @property
    def success(self) -> bool:
        return self.status == "completed"


class PiAgent:
    """
    Pi Agent 适配器 — 复刻 Multica 的 piBackend

    用法:
        agent = PiAgent(pi_path)
        result = agent.execute(prompt="用中文介绍你自己", cwd="/project",
                               model="anthropic/claude-sonnet-4", system_prompt="...")
        print(result.text)       # 纯文本输出
        print(result.thinking)   # 思考过程
        print(result.status)     # completed / failed / timeout / aborted
    """

    def __init__(self, pi_path: str, timeout: int = 7200):
        self.pi_path = pi_path
        self.timeout = timeout
        # 活跃进程注册表：token → Popen，供 cancel() 定位
        self._procs: dict = {}
        self._procs_lock = threading.Lock()

    def execute(self, prompt: str, cwd: str = None, model: str = "",
                system_prompt: str = "", custom_args: list = None,
                resume_session_id: str = "", timeout: int = None,
                cancel_event: threading.Event = None,
                on_event=None) -> PiResult:
        """
        执行 Pi 任务，解析 JSON 事件流（复刻 piBackend.Execute）。

        - resume_session_id: 上一轮返回的 result.session_id，实现多轮 resume
        - cancel_event: 外部置位即中止（对应 Go 的 ctx.Canceled → aborted）
        - on_event: 可选回调，每个解析出的事件 (evt_dict) 都会调用（用于实时日志）
        """
        timeout = timeout or self.timeout

        # ── session 路径（复刻：ResumeSessionID 优先，否则新建）──
        session_path = resume_session_id or new_pi_session_path()
        try:
            ensure_pi_session_file(session_path)
        except OSError as e:
            r = PiResult()
            r.status = "failed"
            r.error = f"pi session file: {e}"
            return r

        args = build_pi_args(prompt, session_path, model=model,
                             system_prompt=system_prompt, custom_args=custom_args)
        argv0, cmd_args = choose_pi_invocation(self.pi_path, args)

        logger.info(f"🚀 Pi execute: cwd={cwd or '.'} model={model or 'default'}")
        logger.debug(f"agent command: {argv0} {' '.join(cmd_args)}")

        result = PiResult()
        result.session_id = session_path
        start_time = time.monotonic()
        process = None
        timed_out = False
        aborted = False
        token = object()

        try:
            # ── 1. 启动进程（stdin 显式 pipe；stderr 独立管道，不混入 JSON 流）──
            popen_kwargs = dict(
                cwd=cwd or str(Path.cwd()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            if IS_WINDOWS:
                # CREATE_NO_WINDOW，对应 Go 的 hideAgentWindow
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen([argv0] + cmd_args, **popen_kwargs)

            # ── 2. 立即关闭 stdin（关键！Multica #2188：EOF 解除 Pi 事件循环阻塞）──
            process.stdin.close()

            with self._procs_lock:
                self._procs[token] = process

            logger.info(f"pi started: pid={process.pid} cwd={opts_cwd(cwd)} model={model or 'default'}")

            # ── 3. stderr 后台读取（对应 Go 的 newLogWriter）──
            stderr_lines = []

            def _read_stderr():
                try:
                    for line in process.stderr:
                        line = line.rstrip()
                        if line:
                            stderr_lines.append(line)
                            logger.info(f"[pi:stderr] {line}")
                except Exception:
                    pass

            stderr_thread = threading.Thread(target=_read_stderr, daemon=True, name="pi-stderr")
            stderr_thread.start()

            # ── 4. 看门狗：超时 / 外部取消 → 杀进程树（对应 ctx deadline + stdout.Close）──
            deadline = start_time + timeout

            def _watchdog():
                nonlocal timed_out, aborted
                while True:
                    if process.poll() is not None:
                        return
                    if cancel_event is not None and cancel_event.is_set():
                        aborted = True
                        logger.warning(f"pi aborted: pid={process.pid}")
                        kill_process_tree(process)
                        return
                    if time.monotonic() > deadline:
                        timed_out = True
                        logger.warning(f"pi timeout after {timeout}s: pid={process.pid}")
                        kill_process_tree(process)
                        return
                    time.sleep(0.5)

            watchdog = threading.Thread(target=_watchdog, daemon=True, name="pi-watchdog")
            watchdog.start()

            # ── 5. 逐行解析 JSON 事件流（复刻 scanner 循环；Go 允许 32MB 行）──
            text_buffer = [""]   # drainPiTextBuffer 的 pending 缓冲
            final_status = "completed"
            final_error = ""

            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if on_event:
                    try:
                        on_event(evt)
                    except Exception:
                        pass

                final_status, final_error = self._process_event(
                    evt, result, text_buffer, model, final_status, final_error)

            # EOF：冲刷 text buffer 中残留（复刻 flushPiTextBuffer）
            d = flush_pi_text_buffer(text_buffer)
            if d:
                result.text += d

            process.wait()
            result.exit_code = process.returncode if process.returncode is not None else -1

            # ── 6. 终态判定（复刻 Go: timeout > canceled > waitErr > 事件内 failed）──
            if timed_out:
                final_status = "timeout"
                final_error = f"pi timed out after {timeout}s"
            elif aborted:
                final_status = "aborted"
                final_error = "execution cancelled"
            elif result.exit_code != 0 and final_status == "completed":
                final_status = "failed"
                tail = "\n".join(stderr_lines[-20:])
                final_error = f"pi exited with error: exit code {result.exit_code}" + \
                              (f"\n{tail}" if tail else "")

            result.status = final_status
            result.error = final_error
            result.duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                f"{'✅' if result.success else '❌'} pi finished: pid={process.pid} "
                f"status={final_status} duration={result.duration_ms}ms")

            return result

        except FileNotFoundError as e:
            result.status = "failed"
            result.error = f"start pi: {e}"
            logger.error(f"❌ Pi 启动失败: {e}")
            return result
        except Exception as e:
            logger.exception("Pi execute failed")
            if process is not None and process.poll() is None:
                kill_process_tree(process)
            result.status = "failed"
            result.error = str(e)
            return result
        finally:
            with self._procs_lock:
                self._procs.pop(token, None)

    def cancel_all(self):
        """杀死该适配器所有活跃进程（daemon 关停时调用）"""
        with self._procs_lock:
            procs = list(self._procs.values())
        for p in procs:
            if p.poll() is None:
                kill_process_tree(p)

    def _process_event(self, evt: dict, result: PiResult, text_buffer: list,
                       opt_model: str, final_status: str, final_error: str) -> tuple:
        """处理单个 JSON 事件 — 复刻 Go 的 switch evt.Type。返回更新后的 (final_status, final_error)。"""
        t = evt.get("type", "")

        if t == "agent_start":
            if result.status == "pending":
                result.status = "running"

        elif t == "turn_start":
            # Go: output.Reset(); textBuffer.Reset()
            # 单轮 -p 模式下 text 即 output；多轮 resume 时保留累计文本更实用，
            # 仅重置 pending 缓冲与 Go 行为对齐的部分（textBuffer）。
            text_buffer[0] = ""

        elif t == "message_update":
            ame = evt.get("assistantMessageEvent")
            if ame:
                ame_type = ame.get("type", "")
                delta = ame.get("delta", "")
                if ame_type == "text_delta":
                    d = drain_pi_text_buffer(text_buffer, delta)
                    if d:
                        result.text += d
                elif ame_type == "thinking_delta":
                    if delta:
                        result.thinking += delta

        elif t == "tool_execution_start":
            args = evt.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            result.tool_calls.append({
                "tool": evt.get("toolName", ""),
                "call_id": evt.get("toolCallId", ""),
                "input": args if isinstance(args, dict) else {},
            })

        elif t == "tool_execution_end":
            result.tool_results.append({
                "call_id": evt.get("toolCallId", ""),
                "output": _decode_pi_string(evt.get("result")),
                "is_error": bool(evt.get("isError", False)),
            })

        elif t == "turn_end":
            # 复刻：累计 per-model token usage
            msg = evt.get("message")
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    msg = None
            if isinstance(msg, dict):
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    model = msg.get("model") or opt_model or "unknown"
                    u = result.usage.setdefault(model, {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": 0, "cache_write_tokens": 0})
                    u["input_tokens"] += usage.get("input", 0)
                    u["output_tokens"] += usage.get("output", 0)
                    u["cache_read_tokens"] += usage.get("cacheRead", 0)
                    u["cache_write_tokens"] += usage.get("cacheWrite", 0)

        elif t == "error":
            err_text = _decode_pi_string(evt.get("message"))
            result.errors.append(err_text)
            if final_status == "completed":
                final_status = "failed"
                final_error = err_text

        elif t == "auto_retry_end":
            if not evt.get("success", False) and final_status == "completed":
                final_status = "failed"
                final_error = evt.get("finalError") or "pi exhausted automatic retries"

        return final_status, final_error


def _decode_pi_string(raw) -> str:
    """复刻 decodePiString/decodePiResult：字符串直接返回，否则返回原始 JSON"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


def opts_cwd(cwd) -> str:
    return cwd or "."
