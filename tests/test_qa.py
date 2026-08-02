#!/usr/bin/env python3
"""test_qa.py — QA 扫描单元测试"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import qa  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def test_all():
    d = Path(tempfile.mkdtemp())

    # 1. 自定义规则检出 print
    f = d / "a.py"
    f.write_text("print('debug')\n", encoding="utf-8")
    rules = d / "qa-rules.yaml"
    rules.write_text(
        "rules:\n  - id: QA001\n    pattern: 'print\\(.*\\)'\n    file_glob: '*.py'\n"
        "    severity: warning\n    blocking: false\n    description: no print\n",
        encoding="utf-8")
    findings = qa.run_custom_rules([f], rules)
    check("检出 print", len(findings) == 1 and findings[0]["code"] == "QA001",
          str(findings))

    # 2. 空路径报告 PASS
    md = qa.generate_qa_report([], test_summary={
        "test_a": {"passed": 1, "failed": 0, "skipped": 0, "duration": 0.1}})
    check("空路径 PASS", "✅ PASS" in md, md[-100:])

    # 3. blocking 规则 → BLOCKED
    f2 = d / "test_x.py"
    f2.write_text('import pytest\n@pytest.mark.skip(reason="x")\ndef t(): pass\n',
                  encoding="utf-8")
    rules2 = d / "qa-rules2.yaml"
    rules2.write_text(
        "rules:\n  - id: QA002\n    pattern: '@pytest\\.mark\\.skip'\n"
        "    file_glob: 'test_*.py'\n    severity: blocker\n    blocking: true\n"
        "    description: no skip\n", encoding="utf-8")
    findings2 = qa.run_custom_rules([f2], rules2)
    check("检出 skip", len(findings2) == 1 and findings2[0]["blocking"] is True,
          str(findings2))
    md2 = qa.generate_qa_report([f2], rules_file=rules2)
    check("BLOCKED 判定", "❌ BLOCKED" in md2, md2[-100:])

    # 4. ruff 真实扫描（已安装）
    rf = qa.run_ruff([f])
    check("ruff 扫描干净文件", rf == [], str(rf)[:80])

    # 5. mypy 缺失安全跳过
    mf = qa.run_mypy([f])
    check("mypy 跳过", mf == [])

    print(f"\n{'='*50}\nQA 测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    test_all()
