"""CLI 与 CI 入口测试。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from datacontract.cli import (
    EXIT_INCOMPATIBLE,
    EXIT_OK,
    EXIT_USAGE,
    evaluate,
    evaluate_paths,
    main,
)
from datacontract.parser import parse_dict

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

COMPATIBLE_OLD = {
    "version": "1",
    "root": {"type": "object", "fields": [
        {"name": "id", "type": "integer"},
        {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
    ]},
}
COMPATIBLE_NEW = {
    "version": "2",
    "root": {"type": "object", "fields": [
        {"name": "id", "type": "integer"},
        {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
        {"name": "nick", "type": "string", "optional": True},
    ]},
}
BREAKING_NEW = {
    "version": "3",
    "root": {"type": "object", "fields": [
        {"name": "id", "type": "string"},  # 类型变化
        {"name": "status", "type": {"type": "string", "enum": ["A", "B", "C"]}},
    ]},
}


class EvaluationApiTest(unittest.TestCase):
    def test_compatible_passes(self):
        ev = evaluate(parse_dict(COMPATIBLE_OLD), parse_dict(COMPATIBLE_NEW))
        self.assertTrue(ev.passed)
        self.assertEqual(ev.exit_code(), EXIT_OK)
        self.assertTrue(ev.report.fully_compatible)

    def test_breaking_fails(self):
        ev = evaluate(parse_dict(COMPATIBLE_OLD), parse_dict(BREAKING_NEW))
        self.assertFalse(ev.passed)
        self.assertEqual(ev.exit_code(), EXIT_INCOMPATIBLE)

    def test_warning_threshold(self):
        ev = evaluate(
            parse_dict(COMPATIBLE_OLD),
            parse_dict(COMPATIBLE_NEW),
            fail_on="warning",
        )
        # 新增可选字段会给前向一条 F001 警告
        self.assertFalse(ev.passed)

    def test_never_threshold_always_passes(self):
        ev = evaluate(
            parse_dict(COMPATIBLE_OLD),
            parse_dict(BREAKING_NEW),
            fail_on="never",
        )
        self.assertTrue(ev.passed)
        self.assertEqual(ev.exit_code(), EXIT_OK)

    def test_json_payload_is_serializable(self):
        ev = evaluate(parse_dict(COMPATIBLE_OLD), parse_dict(BREAKING_NEW))
        payload = ev.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        decoded = json.loads(text)
        self.assertIn("compatibility", decoded)
        self.assertIn("migration", decoded)
        self.assertFalse(decoded["compatibility"]["fully_compatible"])
        self.assertTrue(decoded["migration"]["blocking"])


class CliTest(unittest.TestCase):
    def _write(self, d: str, name: str, payload: dict) -> str:
        p = Path(d) / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_check_compatible_exit_zero(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", COMPATIBLE_OLD)
            new = self._write(d, "new.json", COMPATIBLE_NEW)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["check", old, new])
            self.assertEqual(code, EXIT_OK)
            self.assertIn("放行", buf.getvalue())

    def test_check_breaking_exit_one_json(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", COMPATIBLE_OLD)
            new = self._write(d, "new.json", BREAKING_NEW)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["check", old, new, "--format", "json"])
            self.assertEqual(code, EXIT_INCOMPATIBLE)
            payload = json.loads(buf.getvalue())
            self.assertFalse(payload["passed"])
            self.assertTrue(payload["migration"]["steps"])

    def test_missing_file_exit_two(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["check", "/no/such/old.json", "/no/such/new.json"])
        self.assertEqual(code, EXIT_USAGE)

    def test_invalid_contract_exit_two(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", {"root": "string"})
            bad = self._write(d, "bad.json", {"root": {"type": "string", "enum": []}})
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["check", old, bad])
            self.assertEqual(code, EXIT_USAGE)
            self.assertIn("契约非法", err.getvalue())

    def test_example_pair_compatible(self):
        ev = evaluate_paths(
            EXAMPLES / "user_v1.json",
            EXAMPLES / "user_v2_compatible.json",
        )
        self.assertTrue(ev.report.fully_compatible, msg=json.dumps(ev.to_dict(), ensure_ascii=False))

    def test_example_pair_breaking(self):
        ev = evaluate_paths(
            EXAMPLES / "user_v1.json",
            EXAMPLES / "user_v2_breaking.json",
        )
        self.assertFalse(ev.report.fully_compatible)
        rids = {f.rule_id for f in ev.report.errors()}
        # id 改类型（F021/B021）、枚举扩展（F010）、email 必填新增（B001）、name 删除（F002）
        self.assertIn("F002", rids)
        self.assertIn("B001", rids)
        self.assertIn("F010", rids)


if __name__ == "__main__":
    unittest.main()
