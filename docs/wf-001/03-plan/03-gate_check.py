#!/usr/bin/env python3
"""
03-gate_check.py — wf-001 Stage 3 门控自动检查器

复用 Stage 2 的通用引擎结构：yaml.safe_load 解析 03-gate-checks.yaml，
分发到 structure/content/cross_ref/yaml_parse/human 处理器。
人审决策从 03-human-decisions.yaml 读取（防重跑覆写）。
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

STAGE_DIR = Path(__file__).resolve().parent
REPORT_FILE = STAGE_DIR / "03-plan.md"
RULES_FILE = STAGE_DIR / "03-gate-checks.yaml"
RESULT_FILE = STAGE_DIR / "03-gate-result.md"
DECISIONS_FILE = STAGE_DIR / "03-human-decisions.yaml"


def count_chinese_chars(text: str) -> int:
    return len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2000-\u206f]', text))


def extract_yaml_blocks(text: str) -> list[str]:
    return re.findall(r'```yaml\n(.*?)```', text, re.DOTALL)


def _load_human_decisions() -> dict:
    if not DECISIONS_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(DECISIONS_FILE.read_text(encoding="utf-8"))
        return data.get("decisions", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def check_structure(check: dict, report: str) -> dict:
    cid = check["id"]
    if cid == "P1":
        passed = REPORT_FILE.exists() and REPORT_FILE.stat().st_size > 0
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"],
                "passed": passed,
                "detail": f"size={REPORT_FILE.stat().st_size}B" if passed else "MISSING"}
    expected = check.get("expected_sections", [])
    missing = [s for s in expected if s not in report]
    return {"id": cid, "severity": check["severity"], "type": "structure",
            "rule": check["rule"], "passed": len(missing) == 0,
            "detail": f"missing={missing}" if missing else f"all {len(expected)} present"}


def check_content(check: dict, report: str) -> dict:
    cid = check["id"]
    if cid == "P6":
        # 剥离代码块后再查占位符（代码示例中的正则字面量不算占位符）
        body = re.sub(r'```.*?```', '', report, flags=re.DOTALL)
        placeholders = [r'\bTODO\b', r'\bTBD\b', r'\bFIXME\b', r'待定']
        found = [p for pat in placeholders for p in re.findall(pat, body)]
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": len(found) == 0,
                "detail": f"found={found}" if found else "clean (code blocks excluded)"}
    if cid == "P7":
        chars = count_chinese_chars(report)
        threshold = check.get("min_chars", 3000)
        code_blocks = len(re.findall(r'```\w*\n', report))
        min_blocks = check.get("min_code_blocks", 0)
        passed = chars >= threshold or code_blocks >= min_blocks
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": passed,
                "detail": f"chinese_chars={chars} (threshold={threshold}) | code_blocks={code_blocks} (min={min_blocks})"}
    return {"id": cid, "severity": check["severity"], "type": "content",
            "rule": check["rule"], "passed": False, "detail": "unknown content check"}


def check_cross_ref(check: dict, report: str) -> dict:
    cid = check["id"]
    tokens = check.get("expected_tokens", [])
    missing = [t for t in tokens if t not in report]
    return {"id": cid, "severity": check["severity"], "type": "cross_ref",
            "rule": check["rule"], "passed": len(missing) == 0,
            "detail": f"missing={missing}" if missing else f"all {len(tokens)} present"}


def check_yaml_parse(check: dict, yaml_blocks: list[str]) -> dict:
    cid = check["id"]
    idx = check.get("yaml_block_index", 0)
    if idx >= len(yaml_blocks):
        return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                "rule": check["rule"], "passed": False,
                "detail": f"block index {idx} out of range (total {len(yaml_blocks)})"}
    try:
        yaml.safe_load(yaml_blocks[idx])
        return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                "rule": check["rule"], "passed": True,
                "detail": f"block[{idx}] valid YAML ({len(yaml_blocks[idx])}B)"}
    except yaml.YAMLError as e:
        return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                "rule": check["rule"], "passed": False,
                "detail": f"block[{idx}] YAML error: {e}"}


def check_human(check: dict, report: str) -> dict:
    cid = check["id"]
    machine_checks = check.get("machine_checks", [])
    precheck = {t: t in report for t in machine_checks}
    summary = f"{sum(1 for v in precheck.values() if v)}/{len(precheck)} found"
    decisions = _load_human_decisions()
    if cid in decisions:
        d = decisions[cid]
        return {"id": cid, "severity": check["severity"], "type": "human",
                "rule": check["rule"], "passed": None,
                "detail": f"precheck: {summary} — {precheck}",
                "human_review_decision": d.get("decision", "approved"),
                "human_reviewer": d.get("reviewer", ""),
                "human_review_at": d.get("at", ""),
                "human_review_reason": d.get("reason", "")}
    return {"id": cid, "severity": check["severity"], "type": "human",
            "rule": check["rule"], "passed": None,
            "detail": f"precheck: {summary} — {precheck}",
            "human_review_decision": None, "human_reviewer": None,
            "human_review_at": None, "human_review_reason": None}


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
    blockers = [r for r in machine if r["severity"] == "blocker" and r["passed"] is False]
    warnings = [r for r in machine if r["severity"] == "warning" and r["passed"] is False]
    overall = "PASS" if not blockers else "FAIL"
    pending = [r for r in human if r.get("human_review_decision") is None]
    decided = [r for r in human if r.get("human_review_decision") is not None]
    human_status = f"PENDING ({len(pending)} awaiting)" if pending else f"APPROVED ({len(decided)}/{len(human)})"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Gate Check Result — wf-001 Stage 3 (plan)",
        "",
        f"**检查时间**: {now}",
        f"**报告文件**: `03-plan.md` ({REPORT_FILE.stat().st_size}B)",
        f"**规则文件**: `03-gate-checks.yaml` (yaml.safe_load 真解析)",
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
        lines.append(f"✅ 自动检查：{len(machine) - len(blockers) - len(warnings)} pass, {len(blockers)} block, {len(warnings)} warn")
    else:
        lines.append(f"❌ 自动检查：{len(blockers)} blocker FAILED")
    lines += ["", "## 自动检查明细", "", "| ID | 严重度 | 类型 | 规则 | 结果 | 详情 |", "|----|--------|------|------|------|------|"]
    for r in machine:
        icon = "✅" if r["passed"] else "❌"
        lines.append(f"| {r['id']} | {r['severity']} | {r['type']} | {r['rule']} | {icon} | {r['detail']} |")
    lines += ["", "## 人工审批", "", "> 决策来源: 用户授权代理决策（2026-08-01）。", "",
              "| ID | 规则 | 预检 | 审批决定 | 审批人 | 时间 |", "|----|------|------|---------|--------|------|"]
    for r in human:
        decision = r.get("human_review_decision") or "`<待填写>`"
        reviewer = r.get("human_reviewer") or "`<待填写>`"
        reviewed_at = r.get("human_review_at") or "`<待填写>`"
        lines.append(f"| {r['id']} | {r['rule'][:60]}... | {r['detail']} | {decision} | {reviewer} | {reviewed_at} |")
    lines += ["", "## 签署区", "", "**审批人**: _______________    **日期**: _______________", "",
              "**决定**: ☐ 批准（进入 Stage 4 实施）   ☐ 驳回（说明: ___）", ""]
    return "\n".join(lines) + "\n"


def main():
    if not RULES_FILE.exists() or not REPORT_FILE.exists():
        print("ERROR: rules or report missing", file=sys.stderr)
        sys.exit(1)
    rules = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    report = REPORT_FILE.read_text(encoding="utf-8")
    yaml_blocks = extract_yaml_blocks(report)
    print(f"📋 Rules: {len(rules['checks'])} checks | 📄 Report: {REPORT_FILE.stat().st_size}B, "
          f"{count_chinese_chars(report)} chars | 📦 YAML blocks: {len(yaml_blocks)}")
    results = run_checks(rules, report, yaml_blocks)
    RESULT_FILE.write_text(generate_markdown(results, len(yaml_blocks)), encoding="utf-8")
    machine = [r for r in results if r.get("type") != "human"]
    blockers = [r for r in machine if r["severity"] == "blocker" and not r["passed"]]
    warnings = [r for r in machine if r["severity"] == "warning" and not r["passed"]]
    decided = [r for r in results if r.get("type") == "human" and r.get("human_review_decision")]
    print(f"   Auto: {'PASS' if not blockers else f'{len(blockers)} BLOCKERS'}"
          + (f" | warn: {len(warnings)}" if warnings else "")
          + f" | Human: {len(decided)}/{len([r for r in results if r.get('type')=='human'])} decided")


if __name__ == "__main__":
    main()
