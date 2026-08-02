#!/usr/bin/env python3
"""
04-gate_check.py — wf-001 Stage 4 门控自动检查器

验证实施成果（I1-I5 自动 + H1-H2 代理决策）。
检查器逻辑复用 Stage 3 模式，但 target 指向代码文件（glob 匹配项目根）。
"""
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

STAGE_DIR = Path(__file__).resolve().parent
ROOT = STAGE_DIR.parent.parent.parent  # docs/wf-001/04-implementation → pi-orchestrator
RULES_FILE = STAGE_DIR / "04-gate-checks.yaml"
RESULT_FILE = STAGE_DIR / "04-gate-result.md"
DECISIONS_FILE = STAGE_DIR / "04-human-decisions.yaml"


def _load_human_decisions() -> dict:
    if not DECISIONS_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(DECISIONS_FILE.read_text(encoding="utf-8"))
        return data.get("decisions", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _collect_text(target: str) -> str:
    """按 target glob 收集 ROOT 下匹配文件的全部内容"""
    if target == "人工":
        return ""
    parts = []
    for p in sorted(ROOT.glob(target)):
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def check_structure(check: dict) -> dict:
    cid = check["id"]
    target = check.get("target", "")
    expected = check.get("expected_sections", [])
    if target.startswith("*") or "/" in target:
        # 文件存在性：glob 匹配数 ≥ expected 数
        matches = sorted(ROOT.glob(target))
        names = " ".join(m.name for m in matches if m.is_file())
        missing = [s for s in expected if s not in names]
        passed = len(missing) == 0
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"], "passed": passed,
                "detail": f"found={[m.name for m in matches]} missing={missing}" if missing
                else f"all {len(expected)} present in {len(matches)} files"}
    text = _collect_text(target)
    missing = [s for s in expected if s not in text]
    return {"id": cid, "severity": check["severity"], "type": "structure",
            "rule": check["rule"], "passed": len(missing) == 0,
            "detail": f"missing={missing}" if missing else f"all {len(expected)} present"}


def check_content(check: dict) -> dict:
    cid = check["id"]
    target = check.get("target", "")
    if cid == "I3":
        matches = sorted(ROOT.glob(target))
        names = " ".join(m.stem for m in matches if m.is_file())
        expected = check.get("expected_sections", [])
        missing = [s for s in expected if s not in names]
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else "4 测试文件齐备"}
    if cid == "I4":
        # 断言数：跑每个测试文件统计 check( 调用 + 结果行
        counts = {}
        for t in ["test_workflow.py", "test_gate.py", "test_qa.py", "test_workflow_e2e.py"]:
            tf = ROOT / "tests" / t
            if not tf.exists():
                counts[t] = -1
                continue
            try:
                out = subprocess.run([sys.executable, str(tf)], capture_output=True,
                                     text=True, timeout=180, cwd=ROOT)
                m = re.search(r"(\d+) 通过, (\d+) 失败", out.stdout or "")
                counts[t] = (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
            except Exception:
                counts[t] = (-2, -2)
        ok = (counts.get("test_workflow.py", (-1, 0))[0] >= 10
              and counts.get("test_gate.py", (-1, 0))[0] >= 8
              and counts.get("test_qa.py", (-1, 0))[0] >= 6
              and counts.get("test_workflow_e2e.py", (-1, 0))[0] >= 8
              and all(v[1] == 0 for v in counts.values()))
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": ok,
                "detail": str(counts)}
    return {"id": cid, "severity": check["severity"], "type": "content",
            "rule": check["rule"], "passed": False, "detail": "unknown content check"}


def check_cross_ref(check: dict) -> dict:
    cid = check["id"]
    text = _collect_text(check.get("target", "*.py"))
    tokens = check.get("expected_tokens", [])
    missing = [t for t in tokens if t not in text]
    return {"id": cid, "severity": check["severity"], "type": "cross_ref",
            "rule": check["rule"], "passed": len(missing) == 0,
            "detail": f"missing={missing}" if missing else f"all {len(tokens)} present"}


def check_human(check: dict) -> dict:
    cid = check["id"]
    text = _collect_text("*.py")
    precheck = {t: t in text for t in check.get("machine_checks", [])}
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


def run_checks(rules: dict) -> list[dict]:
    results = []
    for check in rules.get("checks", []):
        t = check.get("type", "")
        try:
            if t == "structure":
                results.append(check_structure(check))
            elif t == "content":
                results.append(check_content(check))
            elif t == "cross_ref":
                results.append(check_cross_ref(check))
            elif t == "human":
                results.append(check_human(check))
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
        "# Gate Check Result — wf-001 Stage 4 (implementation)",
        "",
        f"**检查时间**: {now}",
        f"**规则文件**: `04-gate-checks.yaml` (yaml.safe_load 真解析)",
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
              "**决定**: ☐ 批准（进入 Stage 5 QA）   ☐ 驳回（说明: ___）", ""]
    return "\n".join(lines) + "\n"


def main():
    if not RULES_FILE.exists():
        print("ERROR: rules missing", file=sys.stderr)
        sys.exit(1)
    rules = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    print(f"📋 Rules: {len(rules['checks'])} checks | ROOT: {ROOT}")
    results = run_checks(rules)
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
