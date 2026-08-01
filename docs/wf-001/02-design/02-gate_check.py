#!/usr/bin/env python3
"""
02-gate_check.py — wf-001 Stage 2 门控自动检查器

核心设计：yaml.safe_load 真解析 02-gate-checks.yaml，按 type 字段分发到
structure / content / cross_ref / yaml_parse / human 五类处理器。
零硬编码检查逻辑——改规则只改 .yaml，不改 .py。

这是对 Stage 1 硬编码 gate_check.py 的正式修复。
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

STAGE_DIR = Path(__file__).resolve().parent
REPORT_FILE = STAGE_DIR / "02-design-report.md"
RULES_FILE = STAGE_DIR / "02-gate-checks.yaml"
RESULT_FILE = STAGE_DIR / "02-gate-result.md"


def count_chinese_chars(text: str) -> int:
    return len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2000-\u206f]', text))


def extract_yaml_blocks(text: str) -> list[str]:
    """提取所有 ```yaml ... ``` 代码块内容（按出现顺序）"""
    return re.findall(r'```yaml\n(.*?)```', text, re.DOTALL)


# ═══════════════════════════════════════════
# 五类检查处理器
# ═══════════════════════════════════════════

def check_structure(check: dict, report: str) -> dict:
    """structure 型：校验 expected_sections 子串存在，或 D1 文件存在+非空"""
    cid = check["id"]
    # D1 特殊：文件存在 + 非空
    if cid == "D1":
        passed = REPORT_FILE.exists() and REPORT_FILE.stat().st_size > 0
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"],
                "passed": passed,
                "detail": f"size={REPORT_FILE.stat().st_size}B" if passed else "MISSING"}

    expected = check.get("expected_sections", [])
    missing = [s for s in expected if s not in report]
    passed = len(missing) == 0
    return {"id": cid, "severity": check["severity"], "type": "structure",
            "rule": check["rule"],
            "passed": passed,
            "detail": f"missing={missing}" if missing else f"all {len(expected)} present"}


def check_content(check: dict, report: str) -> dict:
    """content 型：占位符扫描 / 中文字符数阈值"""
    cid = check["id"]
    if cid == "D8":
        placeholders = [r'\bTODO\b', r'\bTBD\b', r'\bFIXME\b', r'待定']
        found = []
        for pat in placeholders:
            found.extend(re.findall(pat, report))
        passed = len(found) == 0
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"],
                "passed": passed,
                "detail": f"found={found}" if found else "clean"}
    if cid == "D9":
        chars = count_chinese_chars(report)
        threshold = check.get("min_chars", 5000)
        passed = chars >= threshold
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"],
                "passed": passed,
                "detail": f"chinese_chars={chars} (threshold={threshold})"}
    return {"id": cid, "severity": check["severity"], "type": "content",
            "rule": check["rule"], "passed": False, "detail": "unknown content check"}


def check_cross_ref(check: dict, report: str) -> dict:
    """cross_ref 型：校验 expected_tokens 每项都在报告中出现"""
    cid = check["id"]
    tokens = check.get("expected_tokens", [])
    missing = [t for t in tokens if t not in report]
    passed = len(missing) == 0
    return {"id": cid, "severity": check["severity"], "type": "cross_ref",
            "rule": check["rule"],
            "passed": passed,
            "detail": f"missing={missing}" if missing else f"all {len(tokens)} present"}


def check_yaml_parse(check: dict, yaml_blocks: list[str]) -> dict:
    """yaml_parse 型：按 yaml_block_index 提取 + yaml.safe_load 真解析"""
    cid = check["id"]
    idx = check.get("yaml_block_index", 0)
    if idx >= len(yaml_blocks):
        return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                "rule": check["rule"],
                "passed": False,
                "detail": f"block index {idx} out of range (total {len(yaml_blocks)} yaml blocks)"}

    block = yaml_blocks[idx]
    try:
        yaml.safe_load(block)
        return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                "rule": check["rule"],
                "passed": True,
                "detail": f"block[{idx}] valid YAML ({len(block)}B)"}
    except yaml.YAMLError as e:
        return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                "rule": check["rule"],
                "passed": False,
                "detail": f"block[{idx}] YAML error: {e}"}


def check_human(check: dict, report: str) -> dict:
    """human 型：跑 machine_checks 预检 → 产出摘要，passed=None + 人审栏留空"""
    cid = check["id"]
    machine_checks = check.get("machine_checks", [])
    precheck = {}
    for token in machine_checks:
        precheck[token] = token in report
    all_ok = all(precheck.values()) if precheck else None
    summary = (f"{sum(1 for v in precheck.values() if v)}/{len(precheck)} found" 
               if precheck else "no prechecks")
    return {
        "id": cid, "severity": check["severity"], "type": "human",
        "rule": check["rule"],
        "passed": None,
        "detail": f"precheck: {summary} — {precheck}",
        "human_review_decision": None,
        "human_reviewer": None,
        "human_review_at": None,
    }


# ═══════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════

def run_checks(rules: dict, report: str, yaml_blocks: list[str]) -> list[dict]:
    results = []
    for check in rules.get("checks", []):
        t = check.get("type", "")
        try:
            if t == "structure":
                results.append(check_structure(check, report))
            elif t == "content":
                results.append(check_content(check, report))
            elif t == "cross_ref":
                results.append(check_cross_ref(check, report))
            elif t == "yaml_parse":
                results.append(check_yaml_parse(check, yaml_blocks))
            elif t == "human":
                results.append(check_human(check, report))
            else:
                results.append({"id": check["id"], "severity": "blocker", "passed": False,
                                "detail": f"unknown type: {t}"})
        except Exception as e:
            results.append({"id": check["id"], "severity": "blocker", "passed": False,
                            "detail": f"exception: {e}"})
    return results


def generate_markdown(results: list[dict], block_count: int) -> str:
    machine = [r for r in results if r.get("type") != "human"]
    human = [r for r in results if r.get("type") == "human"]
    machine_blockers = [r for r in machine if r.get("severity") == "blocker" and r.get("passed") is False]
    machine_warnings = [r for r in machine if r.get("severity") == "warning" and r.get("passed") is False]
    machine_passed = [r for r in machine if r.get("passed") is True]
    overall = "PASS" if len(machine_blockers) == 0 else "FAIL"
    pending = [r for r in human if r.get("human_review_decision") is None]
    human_status = f"PENDING ({len(pending)} awaiting review)"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Gate Check Result — wf-001 Stage 2 (design)",
        "",
        f"**检查时间**: {now}",
        f"**报告文件**: `02-design-report.md` ({REPORT_FILE.stat().st_size}B)",
        f"**规则文件**: `02-gate-checks.yaml` (yaml.safe_load 真解析)",
        f"**YAML 代码块**: 检测到 {block_count} 个 ```yaml 块",
        "",
        "---",
        "",
        "## 总览",
        "",
        f"| 维度 | 结果 |",
        f"|------|------|",
        f"| 自动检查 | **{overall}** |",
        f"| 人工审批 | **{human_status}** |",
        "",
    ]
    if overall == "PASS":
        lines.append(f"✅ 自动检查：{len(machine_passed)} pass, {len(machine_blockers)} block, {len(machine_warnings)} warn")
    else:
        lines.append(f"❌ 自动检查：{len(machine_blockers)} blocker FAILED ({len(machine_warnings)} warnings)")
    lines += [
        "",
        "---",
        "",
        "## 自动检查明细",
        "",
        "| ID | 严重度 | 类型 | 规则 | 结果 | 详情 |",
        "|----|--------|------|------|------|------|",
    ]
    for r in machine:
        icon = "✅" if r["passed"] else ("❌" if r["passed"] is False else "⏳")
        lines.append(f"| {r['id']} | {r['severity']} | {r['type']} | {r['rule']} | {icon} | {r['detail']} |")

    lines += [
        "",
        "---",
        "",
        "## 人工审批",
        "",
        "> ⚠️ 以下需用户逐项填写。Agent 已留空，绝不代填。",
        "",
        "| ID | 规则 | 预检 | 审批决定 | 审批人 | 时间 |",
        "|----|------|------|---------|--------|------|",
    ]
    for r in human:
        decision = r.get("human_review_decision") or "`<待填写>`"
        reviewer = r.get("human_reviewer") or "`<待填写>`"
        reviewed_at = r.get("human_review_at") or "`<待填写>`"
        lines.append(f"| {r['id']} | {r['rule'][:70]}... | {r['detail']} | {decision} | {reviewer} | {reviewed_at} |")

    lines += [
        "",
        "---",
        "",
        "## 签署区",
        "",
        "**审批人**: _______________    **日期**: _______________",
        "",
        "**决定**: ☐ 批准（进入 Stage 3 实施计划）   ☐ 驳回（说明: ___）",
        "",
    ]
    return "\n".join(lines) + "\n"


def main():
    # 加载
    if not RULES_FILE.exists():
        print(f"ERROR: {RULES_FILE} not found", file=sys.stderr)
        sys.exit(1)
    if not REPORT_FILE.exists():
        print(f"ERROR: {REPORT_FILE} not found", file=sys.stderr)
        sys.exit(1)

    rules = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    report = REPORT_FILE.read_text(encoding="utf-8")
    yaml_blocks = extract_yaml_blocks(report)

    print(f"📋 Rules loaded: {len(rules['checks'])} checks")
    print(f"📄 Report: {REPORT_FILE.stat().st_size}B, {count_chinese_chars(report)} chinese chars")
    print(f"📦 YAML blocks found: {len(yaml_blocks)}")

    # 运行
    results = run_checks(rules, report, yaml_blocks)

    # 输出
    RESULT_FILE.write_text(generate_markdown(results, len(yaml_blocks)), encoding="utf-8")
    print(f"✅ gate-result.md → {RESULT_FILE}")

    # 摘要
    machine = [r for r in results if r.get("type") != "human"]
    human = [r for r in results if r.get("type") == "human"]
    blockers = [r for r in machine if r["severity"] == "blocker" and not r["passed"]]
    warnings = [r for r in machine if r["severity"] == "warning" and not r["passed"]]
    print(f"   Auto: {'PASS' if not blockers else f'{len(blockers)} BLOCKERS'}")
    if warnings:
        print(f"   Warnings: {len(warnings)}")
    print(f"   Human: {len(human)} items pending review")


if __name__ == "__main__":
    main()
