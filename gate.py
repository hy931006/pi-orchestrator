"""gate.py — 通用门控引擎（YAML 驱动，零硬编码）

职责（对应 02-design-report.md §6）:
- 规则文件 yaml.safe_load 真解析（R10: 解析失败即 FAIL，不静默 PASS）
- 五类检查: structure / content / cross_ref / yaml_parse / human
- 每个检查异常 → FAIL（R10），不吞异常
- 生成 gate-result.md（结构对齐 02-gate-result.md）
"""
import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger("gate")


class GateError(Exception):
    pass


class GateEngine:
    """通用门控引擎 — 规则文件驱动"""

    def __init__(self, rules_path: Path, artifact_dir: Path):
        if not rules_path.exists():
            raise GateError(f"规则文件不存在: {rules_path}")
        try:
            self.rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise GateError(f"规则文件 YAML 解析失败（R10: 解析失败即 FAIL 而非 PASS）: {e}")
        if not isinstance(self.rules, dict) or "checks" not in self.rules:
            raise GateError(f"规则文件缺少 checks 段: {rules_path}")
        self.artifact_dir = artifact_dir
        self.report_text = ""
        self._target_files = {}
        self._resolve_targets()

    def _resolve_targets(self):
        """按 target glob 匹配 artifact_dir 下文件（取最近修改的）"""
        for check in self.rules.get("checks", []):
            tgt = check.get("target", "")
            if tgt and tgt != "人工":
                matches = sorted(self.artifact_dir.glob(tgt),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
                if matches:
                    self._target_files[check["id"]] = matches[0]
        # 报告文本 = 第一个 structure 型 target 的内容
        for check in self.rules.get("checks", []):
            if check.get("type") == "structure" and check["id"] in self._target_files:
                self.report_text = self._target_files[check["id"]].read_text(
                    encoding="utf-8", errors="replace")
                break

    def run(self) -> list[dict]:
        results = []
        for check in self.rules.get("checks", []):
            try:
                handler = {
                    "structure": self._check_structure,
                    "content": self._check_content,
                    "cross_ref": self._check_cross_ref,
                    "yaml_parse": self._check_yaml_parse,
                    "human": self._check_human,
                }.get(check.get("type"))
                if handler:
                    results.append(handler(check))
                else:
                    results.append({"id": check["id"], "severity": "blocker", "passed": False,
                                    "detail": f"未知检查类型: {check.get('type')}"})
            except Exception as e:
                # R10: 任何异常 → FAIL 而非静默 PASS
                results.append({"id": check["id"], "severity": "blocker", "passed": False,
                                "detail": f"检查异常（按 R10 处理为 FAIL）: {e}"})
        return results

    # ── 五类检查 ──

    def _check_structure(self, check: dict) -> dict:
        cid = check["id"]
        f = self._target_files.get(cid)
        if not f:
            return {"id": cid, "severity": check["severity"], "type": "structure",
                    "rule": check["rule"], "passed": False, "detail": "target 文件不存在"}
        expected = check.get("expected_sections", [])
        if not expected:
            # 无章节要求 → 仅要求文件存在且非空
            passed = f.stat().st_size > 0
            return {"id": cid, "severity": check["severity"], "type": "structure",
                    "rule": check["rule"], "passed": passed,
                    "detail": f"size={f.stat().st_size}B" if passed else "EMPTY"}
        text = f.read_text(encoding="utf-8", errors="replace")
        missing = [s for s in expected if s not in text]
        return {"id": cid, "severity": check["severity"], "type": "structure",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else f"all {len(expected)} present"}

    def _check_content(self, check: dict) -> dict:
        cid = check["id"]
        text = self.report_text
        if not text:
            return {"id": cid, "severity": check["severity"], "type": "content",
                    "rule": check["rule"], "passed": False, "detail": "无报告文本"}
        # 剥离代码块后再查占位符（代码示例中的字面量不算）
        body = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        placeholders = [r'\bTODO\b', r'\bTBD\b', r'\bFIXME\b', r'待定']
        found = [p for pat in placeholders for p in re.findall(pat, body)]
        return {"id": cid, "severity": check["severity"], "type": "content",
                "rule": check["rule"], "passed": len(found) == 0,
                "detail": f"found={found}" if found else "clean"}

    def _check_cross_ref(self, check: dict) -> dict:
        cid = check["id"]
        tokens = check.get("expected_tokens", [])
        text = self.report_text
        missing = [t for t in tokens if t not in text]
        return {"id": cid, "severity": check["severity"], "type": "cross_ref",
                "rule": check["rule"], "passed": len(missing) == 0,
                "detail": f"missing={missing}" if missing else f"all {len(tokens)} present"}

    def _check_yaml_parse(self, check: dict) -> dict:
        cid = check["id"]
        idx = check.get("yaml_block_index", 0)
        blocks = re.findall(r'```yaml\n(.*?)```', self.report_text, re.DOTALL)
        if idx >= len(blocks):
            return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                    "rule": check["rule"], "passed": False,
                    "detail": f"yaml block {idx} 超出范围（共 {len(blocks)} 个）"}
        try:
            yaml.safe_load(blocks[idx])
            return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                    "rule": check["rule"], "passed": True,
                    "detail": f"block[{idx}] valid YAML ({len(blocks[idx])}B)"}
        except yaml.YAMLError as e:
            return {"id": cid, "severity": check["severity"], "type": "yaml_parse",
                    "rule": check["rule"], "passed": False,
                    "detail": f"block[{idx}] YAML error: {e}"}

    def _check_human(self, check: dict) -> dict:
        """人审项：预检 + 占位。决策由外部（审批 API / 决策文件）写入"""
        cid = check["id"]
        precheck = {t: t in self.report_text for t in check.get("machine_checks", [])}
        return {"id": cid, "severity": check["severity"], "type": "human",
                "rule": check["rule"], "passed": None,
                "detail": f"precheck: {sum(1 for v in precheck.values() if v)}/{len(precheck)}",
                "human_review_decision": None, "human_reviewer": None,
                "human_review_at": None}

    # ── 报告生成 ──

    def generate_markdown(self, results: list[dict]) -> str:
        machine = [r for r in results if r.get("type") != "human"]
        human = [r for r in results if r.get("type") == "human"]
        blockers = [r for r in machine if r["severity"] == "blocker" and r["passed"] is False]
        warnings = [r for r in machine if r["severity"] == "warning" and r["passed"] is False]
        overall = "PASS" if not blockers else "FAIL"
        lines = [
            f"# Gate Check Result — {self.artifact_dir.name}",
            "",
            "## 总览",
            "",
            "| 维度 | 结果 |",
            "|------|------|",
            f"| 自动检查 | **{overall}** |",
            "| 人工审批 | **PENDING** |",
            "",
        ]
        if overall == "PASS":
            passed = len(machine) - len(blockers) - len(warnings)
            lines.append(f"✅ 自动检查：{passed} pass, {len(blockers)} block, {len(warnings)} warn")
        else:
            lines.append(f"❌ 自动检查：{len(blockers)} blocker FAILED")
        lines += ["", "## 自动检查明细", "",
                  "| ID | 严重度 | 类型 | 规则 | 结果 | 详情 |",
                  "|----|--------|------|------|------|------|"]
        for r in machine:
            icon = "✅" if r["passed"] else "❌"
            lines.append(f"| {r['id']} | {r['severity']} | {r['type']} | {r['rule']} | {icon} | {r['detail']} |")
        lines += ["", "## 人工审批", "", "| ID | 规则 | 预检 | 审批决定 |",
                  "|----|------|------|---------|"]
        for r in human:
            lines.append(f"| {r['id']} | {r['rule'][:60]}... | {r['detail']} | `<待填写>` |")
        return "\n".join(lines) + "\n"


def run_gate(rules_path: Path, artifact_dir: Path) -> tuple[bool, str]:
    """执行门控，返回 (是否通过, gate-result.md 内容)。

    通过 = 无 blocker 失败（warning 不阻断）。异常由 GateEngine 内部转为 FAIL。
    """
    engine = GateEngine(rules_path, artifact_dir)
    results = engine.run()
    md = engine.generate_markdown(results)
    result_file = artifact_dir / "gate-result.md"
    result_file.write_text(md, encoding="utf-8")
    machine = [r for r in results if r.get("type") != "human"]
    blockers = [r for r in machine if r["severity"] == "blocker" and r["passed"] is False]
    return (len(blockers) == 0, md)
