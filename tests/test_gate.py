#!/usr/bin/env python3
"""test_gate.py — 门控引擎单元测试"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import gate

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def make_rules(d, content, name="gate-checks.yaml"):
    f = d / name
    f.write_text(content, encoding="utf-8")
    return f


def test_all():
    d = Path(tempfile.mkdtemp())
    (d / "report.md").write_text(
        "# 报告\n\n## 现状分析\n## 需求分析\n## 技术方案\n## 风险矩阵\n## 结论\n\n"
        "```yaml\nname: ok\nstages: []\n```\n",
        encoding="utf-8")

    # 1. 四类检查全过
    rules = make_rules(d,
        "schema_version: 1\nchecks:\n"
        "  - id: G1\n    type: structure\n    severity: blocker\n    target: report.md\n"
        "    rule: 章节\n    expected_sections: [现状分析, 需求分析, 技术方案, 风险矩阵, 结论]\n"
        "  - id: G2\n    type: content\n    severity: warning\n    target: report.md\n    rule: 无占位\n"
        "  - id: G3\n    type: cross_ref\n    severity: blocker\n    target: report.md\n"
        "    rule: 标记\n    expected_tokens: [结论]\n"
        "  - id: G4\n    type: yaml_parse\n    severity: blocker\n    target: report.md\n"
        "    rule: yaml\n    yaml_block_index: 0\n")
    engine = gate.GateEngine(rules, d)
    results = engine.run()
    check("4 类检查全过", all(r["passed"] for r in results),
          str([(r["id"], r["passed"]) for r in results]))

    # 2. 坏规则文件 → GateError（R10）
    bad = d / "bad.yaml"
    bad.write_text("checks: [unclosed", encoding="utf-8")
    try:
        gate.GateEngine(bad, d)
        check("坏规则被拒", False)
    except gate.GateError:
        check("坏规则被拒", True)

    # 3. 缺文件 → FAIL
    rules2 = make_rules(d,
        "schema_version: 1\nchecks:\n  - id: X1\n    type: structure\n    severity: blocker\n"
        "    target: nonexistent.md\n    rule: 缺文件\n", name="gate-checks2.yaml")
    res2 = gate.GateEngine(rules2, d).run()
    check("缺文件 FAIL", res2[0]["passed"] is False, res2[0]["detail"])

    # 4. run_gate 端到端
    passed, md = gate.run_gate(rules, d)
    check("run_gate 通过", passed)
    check("gate-result.md 生成", (d / "gate-result.md").exists())
    check("报告含 G1", "G1" in md)

    # 5. 缺失章节 → FAIL
    rules3 = make_rules(d,
        "schema_version: 1\nchecks:\n  - id: G9\n    type: structure\n    severity: blocker\n"
        "    target: report.md\n    rule: 缺章节\n    expected_sections: [不存在的章节]\n",
        name="gate-checks3.yaml")
    res3 = gate.GateEngine(rules3, d).run()
    check("缺章节 FAIL", res3[0]["passed"] is False)

    # 6. human 占位
    rules4 = make_rules(d,
        "schema_version: 1\nchecks:\n  - id: H1\n    type: human\n    severity: blocker\n"
        "    target: 人工\n    rule: 人审\n    machine_checks: [结论]\n", name="gate-checks4.yaml")
    res4 = gate.GateEngine(rules4, d).run()
    check("human decision=None", res4[0]["human_review_decision"] is None)

    print(f"\n{'='*50}\nGate 测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    test_all()
