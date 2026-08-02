#!/usr/bin/env python3
"""
05-gate_check.py — wf-001 Stage 5 门控自动检查器（QA 阶段）

验证 QA 报告完整性（Q1-Q6 自动 + H1-H2 代理决策）。
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

STAGE_DIR = Path(__file__).resolve().parent
REPORT_FILE = STAGE_DIR / "05-qa-report.md"
RULES_FILE = STAGE_DIR / "05-gate-checks.yaml"
RESULT_FILE = STAGE_DIR / "05-gate-result.md"
DECISIONS_FILE = STAGE_DIR / "05-human-decisions.yaml"


def _load_human_decisions() -> dict:
    if not DECISIONS_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(DECISIONS_FILE.read_text(encoding="utf-8"))
        return data.get("decisions", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_report() -> str:
    if not REPORT_FILE.exists():
        return ""
    return REPORT_FILE.read_text(encoding="utf-8", errors="replace")


def check_structure(check: dict, report: str) -> dict:
    cid = check["id"]
    if cid == "Q1":
        expected = ["测试套件", "静态扫描", "自定义规则扫描", "阻断总结"]
        missing = [s for s in expected if s not in report]
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else "4 章节齐备"}
    if cid == "Q2":
        expected = ["test_agent.py", "test_workflow.py", "test_gate.py",
                    "test_qa.py", "test_workflow_e2e.py"]
        missing = [s for s in expected if s not in report]
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else "5 测试文件齐备"}
    return {"id": cid, "severity": check["severity"], "type": "structure",
            "rule": check["rule"], "passed": False, "detail": "unknown structure check"}


def check_content(check: dict, report: str) -> dict:
    cid = check["id"]
    if cid == "Q3":
        passed = "✅ PASS" in report
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": passed,
                "detail": "✅ PASS found" if passed else "❌ BLOCKED or missing"}
    if cid == "Q4":
        passed = "阻断项: 0" in report
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": passed,
                "detail": "阻断项: 0" if passed else "存在阻断项"}
    if cid == "Q6":
        passed = "wf-002" in report and "自举" in report
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": passed,
                "detail": "自举章节存在" if passed else "缺少 wf-002 自举记录"}
    return {"id": cid, "severity": check["severity"], "type": "content",
            "rule": check["rule"], "passed": False, "detail": "unknown content check"}


def check_cross_ref(check: dict, report: str) -> dict:
    cid = check["id"]
    tokens = check.get("expected_tokens", [])
    missing = [t for t in tokens if t not in report]
    return {"id": cid, "severity": check["severity"], "type": "cross_ref",
            "rule": check["rule"], "passed": len(missing) == 0,
            "detail": f"missing={missing}" if missing else f"all {len(tokens)} present"}


def check_human(check: dict, report: str) -> dict:
    cid = check["id"]
    precheck = {t: t in report for t in check.get("machine_checks", [])}
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


def run_checks(rules: dict, report: str) -> list[dict]:
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
            elif t == "human":
                results.append(check_human(check, report))
            else:
                results.append({"id": check["id"], "severity": "blocker", "passed": False,
                                "detail": f"unknown type: {t}"})
        except Exception as e:
            results.append({"id": check["id"], "severity": "blocker", "passed": False,
                            "detail": f"exception: {e}"})
    return results


def generate_markdown(results: list[dict]) -> str:
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
        "# Gate Check Result — wf-001 Stage 5 (qa)",
        "",
        f"**检查时间**: {now}",
        f"**QA 报告**: `05-qa-report.md`",
        "",
        "## 总览",
        "",
        "| 维度 | 结果 |",
        "|------|------|",
        f"| 自动检查 | **{overall}** |",
        f"| 人工审批 | **{human_status}** |",
        "",
    ]
    passed_n = len(machine) - len(blockers) - len(warnings)
    lines.append(f"{'✅' if overall == 'PASS' else '❌'} 自动检查：{passed_n} pass, "
                 f"{len(blockers)} block, {len(warnings)} warn")
    lines += ["", "## 自动检查明细", "",
              "| ID | 严重度 | 类型 | 规则 | 结果 | 详情 |",
              "|----|--------|------|------|------|------|"]
    for r in machine:
        icon = "✅" if r["passed"] else "❌"
        lines.append(f"| {r['id']} | {r['severity']} | {r['type']} | {r['rule']} | {icon} | {r['detail']} |")
    lines += ["", "## 人工审批", "", "> 决策来源: 用户授权代理决策（2026-08-01）。", "",
              "| ID | 规则 | 预检 | 审批决定 | 审批人 | 时间 |",
              "|----|------|------|---------|--------|------|"]
    for r in human:
        decision = r.get("human_review_decision") or "`<待填写>`"
        reviewer = r.get("human_reviewer") or "`<待填写>`"
        reviewed_at = r.get("human_review_at") or "`<待填写>`"
        lines.append(f"| {r['id']} | {r['rule'][:60]}... | {r['detail']} | {decision} | {reviewer} | {reviewed_at} |")
    lines += ["", "## 签署区", "", "**审批人**: _______________    **日期**: _______________", "",
              "**决定**: ☐ 批准（wf-001 完成）   ☐ 驳回（说明: ___）", ""]
    return "\n".join(lines) + "\n"


def main():
    if not RULES_FILE.exists():
        print("ERROR: rules missing", file=sys.stderr)
        sys.exit(1)
    rules = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    report = _read_report()
    print(f"📋 Rules: {len(rules['checks'])} checks | QA report: {len(report)}B")
    results = run_checks(rules, report)
    RESULT_FILE.write_text(generate_markdown(results), encoding="utf-8")
    machine = [r for r in results if r.get("type") != "human"]
    blockers = [r for r in machine if r["severity"] == "blocker" and not r["passed"]]
    warnings = [r for r in machine if r["severity"] == "warning" and not r["passed"]]
    decided = [r for r in results if r.get("type") == "human" and r.get("human_review_decision")]
    print(f"   Auto: {'PASS' if not blockers else f'{len(blockers)} BLOCKERS'}"
          + (f" | warn: {len(warnings)}" if warnings else "")
          + f" | Human: {len(decided)}/{len([r for r in results if r.get('type')=='human'])} decided")


if __name__ == "__main__":
    main()
