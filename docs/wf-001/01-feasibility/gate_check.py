#!/usr/bin/env python3
"""
gate_check.py — wf-001 Stage 门控自动检查器（Stage 1: feasibility）
零依赖（仅 stdlib）。硬编码 F1-F7 自动检查 + H1-H3 人工审批占位。

警告: 本实现为硬编码检查逻辑，不解析 gate-checks.yaml。
      改规则时须两处同步: gate-check.yaml(规格) + 本文件(实现)。

用法:
  python docs/wf-001/01-feasibility/gate_check.py
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STAGE_DIR = Path(__file__).resolve().parent
REPORT_FILE = STAGE_DIR / "01-feasibility-report.md"
RESULT_FILE = STAGE_DIR / "gate-result.md"


def load_report() -> str:
    if not REPORT_FILE.exists():
        raise FileNotFoundError(f"{REPORT_FILE} not found")
    return REPORT_FILE.read_text(encoding="utf-8")


def count_chinese_chars(text: str) -> int:
    """粗略统计中文字符数（含中文标点）"""
    return len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2000-\u206f]', text))


def run_checks(report: str) -> list[dict]:
    results = []

    # F1: 文件存在且非空
    f1_pass = REPORT_FILE.exists() and REPORT_FILE.stat().st_size > 0
    results.append({
        "id": "F1", "severity": "blocker", "type": "structure",
        "rule": "01-feasibility-report.md 文件存在且非空",
        "passed": f1_pass,
        "detail": f"size={REPORT_FILE.stat().st_size}B" if f1_pass else "MISSING"
    })

    # F2: 5 章标题
    required_sections = ["现状分析", "需求分析", "技术方案", "风险矩阵", "结论"]
    f2_pass = all(s in report for s in required_sections)
    missing_sections = [s for s in required_sections if s not in report]
    results.append({
        "id": "F2", "severity": "blocker", "type": "structure",
        "rule": "5 个必须章节标题齐全",
        "passed": f2_pass,
        "detail": f"missing={missing_sections}" if missing_sections else "all 5 present"
    })

    # F3: 风险矩阵含 R1/R2/R3（仅检查 §4 风险矩阵章节，非全文）
    risk_section = ""
    risk_start = report.find("## 4. 风险矩阵")
    if risk_start >= 0:
        conclusion_start = report.find("## 5. 结论", risk_start)
        risk_section = report[risk_start:conclusion_start] if conclusion_start > 0 else report[risk_start:]
    r1 = "pytest" in risk_section
    r2 = "sonar-scanner" in risk_section or "sonar" in risk_section.lower()
    r3 = "checkstyle" in risk_section
    f3_pass = r1 and r2 and r3
    results.append({
        "id": "F3", "severity": "blocker", "type": "structure",
        "rule": "风险矩阵含 pytest/sonar-scanner/checkstyle 环境缺失记录",
        "passed": f3_pass,
        "detail": f"pytest={'✓' if r1 else '✗'} sonar={'✓' if r2 else '✗'} checkstyle={'✓' if r3 else '✗'}"
    })

    # F4: 无占位符
    placeholder_patterns = [r'\bTODO\b', r'\bTBD\b', r'\bFIXME\b', r'待定']
    has_placeholders = False
    found_placeholders = []
    for pat in placeholder_patterns:
        matches = re.findall(pat, report)
        if matches:
            has_placeholders = True
            found_placeholders.extend(matches)
    f4_pass = not has_placeholders
    results.append({
        "id": "F4", "severity": "warning", "type": "content",
        "rule": "无 TODO/TBD/FIXME/待定 占位符",
        "passed": f4_pass,
        "detail": f"found={found_placeholders}" if found_placeholders else "clean"
    })

    # F5: 正文 ≥ 2000 中文字符
    chars = count_chinese_chars(report)
    f5_pass = chars >= 2000
    results.append({
        "id": "F5", "severity": "warning", "type": "content",
        "rule": "正文 ≥ 2000 中文字符",
        "passed": f5_pass,
        "detail": f"chinese_chars={chars}"
    })

    # F6: Q1-Q10 全部有记录（正则精确匹配粗体 Q 标记，消除子串歧义）
    found_qs = set(re.findall(r'\*\*Q(\d+)\*\*', report))
    missing_q = sorted({str(i) for i in range(1, 11)} - found_qs, key=int)
    f6_pass = len(missing_q) == 0
    results.append({
        "id": "F6", "severity": "blocker", "type": "cross_ref",
        "rule": "Q1-Q10 全部 10 个决策点有对应记录",
        "passed": f6_pass,
        "detail": f"missing={missing_q}" if missing_q else "all 10 present"
    })

    # F7: 基线 commit hash 存在
    f7_pass = "8efdf07" in report
    results.append({
        "id": "F7", "severity": "warning", "type": "cross_ref",
        "rule": "基线 commit hash (8efdf07) 已标注",
        "passed": f7_pass,
        "detail": "found" if f7_pass else "not found"
    })

    # H1/H2/H3: 人工审批 — 只产生占位条目，不执行检查
    for hid, rule_text in [
        ("H1", "用户确认 Q1-Q10 需求转述准确"),
        ("H2", "用户接受环境风险清单（R1/R2/R3）"),
        ("H3", "用户批准「扩展 pi-orchestrator」技术方向"),
    ]:
        results.append({
            "id": hid, "severity": "blocker", "type": "human",
            "rule": rule_text,
            "passed": None,       # 待人工判定
            "detail": "待用户审批",
            "human_review_decision": None,
            "human_reviewer": None,
            "human_review_at": None,
        })

    return results


def generate_markdown(results: list[dict]) -> str:
    blockers = [r for r in results if r["severity"] == "blocker"]
    warnings = [r for r in results if r["severity"] == "warning"]
    machine_checks = [r for r in results if r.get("type") != "human"]
    human_checks = [r for r in results if r.get("type") == "human"]

    machine_blockers = [r for r in machine_checks if r["severity"] == "blocker" and r.get("passed") is False]
    machine_warnings = [r for r in machine_checks if r["severity"] == "warning" and r.get("passed") is False]
    machine_passed = [r for r in machine_checks if r.get("passed") is True]

    overall_machine = "PASS" if len(machine_blockers) == 0 else "FAIL"
    pending_human = [r for r in human_checks if r.get("human_review_decision") is None]
    overall_human = f"PENDING ({len(pending_human)} items awaiting review)"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Gate Check Result — wf-001 Stage 1 (feasibility)",
        "",
        f"**检查时间**: {now}",
        f"**报告文件**: `01-feasibility-report.md`",
        f"**规则文件**: `gate-checks.yaml`",
        "",
        "---",
        "",
        "## 总览",
        "",
        f"| 维度 | 结果 |",
        f"|------|------|",
        f"| 机器自动检查 | **{overall_machine}** |",
        f"| 人工审批 | **{overall_human}** |",
        "",
    ]

    if overall_machine == "PASS":
        lines.append(f"✅ 自动检查：{len(machine_passed)} 项通过, {len(machine_blockers)} block, {len(machine_warnings)} warn")
    else:
        lines.append(f"❌ 机器检查：{len(machine_blockers)} 项阻断未通过")

    lines += [
        "",
        "---",
        "",
        "## 机器检查明细",
        "",
        "| ID | 严重度 | 类型 | 规则 | 结果 | 详情 |",
        "|----|--------|------|------|------|------|",
    ]

    for r in machine_checks:
        status_icon = "✅" if r["passed"] else "❌"
        lines.append(f"| {r['id']} | {r['severity']} | {r['type']} | {r['rule']} | {status_icon} | {r['detail']} |")

    lines += [
        "",
        "---",
        "",
        "## 人工审批",
        "",
        "> ⚠️ 以下项目需用户逐项填写。**Agent 已留空，绝不代填。**",
        "",
        "| ID | 严重度 | 规则 | 审批决定 | 审批人 | 审批时间 |",
        "|----|--------|------|---------|--------|---------|",
    ]

    for r in human_checks:
        decision = r.get("human_review_decision") or "`<待填写>`"
        reviewer = r.get("human_reviewer") or "`<待填写>`"
        reviewed_at = r.get("human_review_at") or "`<待填写>`"
        lines.append(f"| {r['id']} | blocker | {r['rule']} | {decision} | {reviewer} | {reviewed_at} |")

    lines += [
        "",
        "---",
        "",
        "## 签署区",
        "",
        "**审批人**: _______________ &nbsp;&nbsp;&nbsp; **日期**: _______________",
        "",
        "**决定**: ☐ 批准（进入 Stage 2 详细设计） &nbsp;&nbsp; ☐ 驳回（说明理由: ___）",
        "",
    ]

    return "\n".join(lines) + "\n"


def main():
    try:
        report = load_report()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    results = run_checks(report)
    md = generate_markdown(results)

    RESULT_FILE.write_text(md, encoding="utf-8")
    print(f"✅ gate-result.md written to {RESULT_FILE}")

    # summary
    machine_blockers = [r for r in results if r["severity"] == "blocker" and r.get("type") != "human" and r.get("passed") is False]
    human_pending = [r for r in results if r.get("type") == "human" and r.get("human_review_decision") is None]
    print(f"   Machine: {'PASS' if not machine_blockers else f'{len(machine_blockers)} blockers'}")
    print(f"   Human:   {len(human_pending)} items pending review")


if __name__ == "__main__":
    main()
