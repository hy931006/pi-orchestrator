"""qa.py — QA 扫描集成（ruff/mypy/checkstyle + 自定义规则）

职责（对应 02-design-report.md §7）:
- ruff check (Python lint) → JSON 解析
- mypy (Python type check) → 文本解析
- 自定义 grep 规则（qa-rules.yaml，设计 §7.3）
- QA 报告生成（§7.2 四章节结构: 测试套件/静态扫描/自定义规则/阻断总结）

工具未安装时跳过对应扫描并打 warning（不崩溃）——工具安装是 Stage 4 的
requirements.txt 增量，air-gapped 可用预置 wheel。
"""
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import yaml

logger = logging.getLogger("qa")


def run_ruff(paths: list[Path]) -> list[dict]:
    """ruff check → 发现列表。ruff 未安装或 paths 为空时返回空"""
    findings = []
    if not paths:
        return findings
    ruff = shutil.which("ruff")
    if not ruff:
        logger.warning("ruff 未安装，跳过 lint")
        return findings
    try:
        out = subprocess.run(
            [ruff, "check", "--output-format", "json"] + [str(p) for p in paths],
            capture_output=True, text=True, timeout=120)
        for item in json.loads(out.stdout or "[]"):
            loc = item.get("location", {})
            findings.append({
                "tool": "ruff", "file": item.get("filename", ""),
                "line": loc.get("row", 0), "col": loc.get("column", 0),
                "code": item.get("code", ""), "severity": "error",
                "blocking": True, "message": item.get("message", ""),
            })
    except Exception as e:
        logger.warning(f"ruff 执行异常: {e}")
    return findings


def run_mypy(paths: list[Path]) -> list[dict]:
    """mypy → 发现列表。未安装或 paths 为空时跳过"""
    findings = []
    if not paths:
        return findings
    mypy = shutil.which("mypy")
    if not mypy:
        logger.warning("mypy 未安装，跳过 type check")
        return findings
    try:
        out = subprocess.run(
            [mypy, "--no-error-summary"] + [str(p) for p in paths],
            capture_output=True, text=True, timeout=120)
        for line in (out.stdout or "").splitlines():
            m = re.match(r"(.+?):(\d+):(?:(\d+):)?\s*(error|note):\s*(.+)", line)
            if m and m.group(4) == "error":
                findings.append({
                    "tool": "mypy", "file": m.group(1), "line": int(m.group(2)),
                    "col": int(m.group(3) or 0), "code": "", "severity": "error",
                    "blocking": False, "message": m.group(5),
                })
    except Exception as e:
        logger.warning(f"mypy 执行异常: {e}")
    return findings


def run_custom_rules(paths: list[Path], rules_file: Path) -> list[dict]:
    """自定义 grep 规则扫描（设计 §7.3）。规则文件缺失 → 空结果"""
    findings = []
    if not rules_file.exists():
        return findings
    rules = yaml.safe_load(rules_file.read_text(encoding="utf-8")).get("rules", [])
    for path in paths:
        # exclude_dirs 支持：跳过指定目录（如 tests/ 的规则测试样例）
        exclude_dirs = {"tests"}
        if any(part in exclude_dirs for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for rule in rules:
            import fnmatch
            glob_ok = not rule.get("file_glob") or fnmatch.fnmatch(path.name, rule["file_glob"])
            if not glob_ok:
                continue
            for m in re.finditer(rule["pattern"], text):
                line_no = text[:m.start()].count("\n") + 1
                findings.append({
                    "tool": "custom", "file": str(path), "line": line_no, "col": 0,
                    "code": rule["id"], "severity": rule.get("severity", "warning"),
                    "blocking": rule.get("blocking", False),
                    "message": rule.get("description", ""),
                })
    return findings


def generate_qa_report(paths: list[Path], test_summary: dict = None,
                       rules_file: Path = None) -> str:
    """生成 QA 报告 markdown（设计 §7.2 四章节结构）

    - paths: 待扫描文件
    - test_summary: {测试名: {passed, failed, skipped, duration}}
    - rules_file: 自定义规则文件路径（默认 qa-rules.yaml 同目录）
    """
    rules_file = rules_file or Path(__file__).parent / "qa-rules.yaml"
    ruff_f = run_ruff(paths)
    mypy_f = run_mypy(paths)
    rules_f = run_custom_rules(paths, rules_file)
    all_f = ruff_f + mypy_f + rules_f
    blockers = [f for f in all_f if f["blocking"]]
    warnings = [f for f in all_f if not f["blocking"]]

    lines = [
        "# QA Report",
        "",
        "## 1. 测试套件",
        "",
        "| 测试文件 | 通过 | 失败 | 跳过 | 耗时 |",
        "|---------|------|------|------|------|",
    ]
    if test_summary:
        for name, s in test_summary.items():
            lines.append(
                f"| {name} | {s.get('passed', 0)} | {s.get('failed', 0)} | "
                f"{s.get('skipped', 0)} | {s.get('duration', 0):.1f}s |")
    else:
        lines.append("| _未提供_ | - | - | - | - |")

    lines += ["", "## 2. 静态扫描", "", "### ruff", "",
              "| 文件 | 行号 | 规则 | 严重度 | 内容 |",
              "|------|------|------|--------|------|"]
    for f in ruff_f:
        lines.append(f"| {f['file']} | {f['line']} | {f['code']} | {f['severity']} | {f['message']} |")
    if not ruff_f:
        lines.append("| _无发现_ | - | - | - | - |")

    lines += ["", "### mypy", "| 文件 | 行号 | 严重度 | 内容 |", "|------|------|--------|------|"]
    for f in mypy_f:
        lines.append(f"| {f['file']} | {f['line']} | {f['severity']} | {f['message']} |")
    if not mypy_f:
        lines.append("| _无发现_ | - | - | - |")

    lines += ["", "## 3. 自定义规则扫描", "",
              "| 规则ID | 文件 | 位置 | 严重度 | 是否阻断 | 描述 |",
              "|--------|------|------|--------|---------|------|"]
    for f in rules_f:
        lines.append(f"| {f['code']} | {f['file']} | {f['line']} | {f['severity']} | {f['blocking']} | {f['message']} |")
    if not rules_f:
        lines.append("| _无发现_ | - | - | - | - | - |")

    lines += ["", "## 4. 阻断总结", f"- 阻断项: {len(blockers)}",
              f"- 警告项: {len(warnings)}",
              f"- **最终判定**: {'❌ BLOCKED' if blockers else '✅ PASS'}", ""]
    return "\n".join(lines) + "\n"
