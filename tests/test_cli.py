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


class EvaluateCommandTest(unittest.TestCase):
    """evaluate 子命令：多下游治理评估的 CI 入口。"""

    OLD = {
        "version": "1",
        "root": {"type": "object", "fields": [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "string"},
        ]},
    }
    NEW_ADDS_OPTIONAL = {
        "version": "2",
        "root": {"type": "object", "fields": [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "string"},
            {"name": "extra", "type": "string", "optional": True},
        ]},
    }
    NEW_REMOVES_NAME = {
        "version": "2",
        "root": {"type": "object", "fields": [
            {"name": "id", "type": "integer"},
        ]},
    }

    def _write(self, d: str, name: str, payload: dict) -> str:
        p = Path(d) / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def _consumer(self, name: str, fields: list, **flags) -> dict:
        payload = {
            "name": name,
            "contract": {"root": {"type": "object", "fields": fields}},
        }
        payload.update(flags)
        return payload

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_compatible_release_exit_zero(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_ADDS_OPTIONAL)
            c1 = self._write(d, "c1.json", self._consumer(
                "pay", [{"name": "id", "type": "integer"}], critical=True))
            code, out, _ = self._run(["evaluate", old, new, "--consumer", c1])
            self.assertEqual(code, EXIT_OK)
            self.assertIn("放行", out)
            self.assertIn("pay", out)

    def test_critical_consumer_broken_exit_one(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_REMOVES_NAME)
            c1 = self._write(d, "c1.json", self._consumer(
                "pay", [{"name": "name", "type": "string"}], critical=True))
            code, out, _ = self._run(
                ["evaluate", old, new, "--consumer", c1, "--format", "json"])
            self.assertEqual(code, EXIT_INCOMPATIBLE)
            payload = json.loads(out)
            self.assertEqual(payload["verdict"], "blocked")
            self.assertFalse(payload["passed"])
            consumers = {c["name"]: c for c in payload["consumers"]}
            self.assertFalse(consumers["pay"]["compatible"])
            self.assertTrue(payload["blocking_reasons"])
            # 迁移步骤落到了具体下游
            actors = {s["actor"] for s in payload["plan"]["steps"]}
            self.assertIn("consumer:pay", actors)
            self.assertIn("producer", actors)

    def test_only_non_critical_broken_exit_zero_by_default(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_REMOVES_NAME)
            c1 = self._write(d, "c1.json", self._consumer(
                "analytics", [{"name": "name", "type": "string"}]))
            code, out, _ = self._run(["evaluate", old, new, "--consumer", c1])
            self.assertEqual(code, EXIT_OK)
            self.assertIn("仅非关键下游受影响", out)

            code, _, _ = self._run(
                ["evaluate", old, new, "--consumer", c1, "--fail-on", "any"])
            self.assertEqual(code, EXIT_INCOMPATIBLE)

    def test_consumers_directory_loads_all_profiles(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_REMOVES_NAME)
            cdir = Path(d) / "consumers"
            cdir.mkdir()
            self._write(str(cdir), "a.json", self._consumer(
                "svc-a", [{"name": "id", "type": "integer"}]))
            self._write(str(cdir), "b.json", self._consumer(
                "svc-b", [{"name": "name", "type": "string"}], critical=True))
            code, out, _ = self._run(
                ["evaluate", old, new, "--consumers", str(cdir), "--format", "json"])
            self.assertEqual(code, EXIT_INCOMPATIBLE)
            payload = json.loads(out)
            names = {c["name"] for c in payload["consumers"]}
            self.assertEqual(names, {"svc-a", "svc-b"})

    def test_no_consumers_still_evaluates_producer(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_ADDS_OPTIONAL)
            code, out, _ = self._run(["evaluate", old, new])
            self.assertEqual(code, EXIT_OK)
            self.assertIn("未提供下游画像", out)

    def test_invalid_consumer_profile_exit_two(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_ADDS_OPTIONAL)
            bad = self._write(d, "bad.json", {"critical": True})  # 缺 name/contract
            code, _, err = self._run(["evaluate", old, new, "--consumer", bad])
            self.assertEqual(code, EXIT_USAGE)
            self.assertIn("输入非法", err)

    def test_missing_consumer_file_exit_two(self):
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", self.OLD)
            new = self._write(d, "new.json", self.NEW_ADDS_OPTIONAL)
            code, _, err = self._run(
                ["evaluate", old, new, "--consumer", "/no/such/consumer.json"])
            self.assertEqual(code, EXIT_USAGE)

    def test_check_command_still_works(self):
        # 旧入口不受新命令影响
        with TemporaryDirectory() as d:
            old = self._write(d, "old.json", COMPATIBLE_OLD)
            new = self._write(d, "new.json", COMPATIBLE_NEW)
            code, out, _ = self._run(["check", old, new])
            self.assertEqual(code, EXIT_OK)
            self.assertIn("放行", out)

    def test_example_consumers_directory_breaking(self):
        # examples/ 下的完整示例：breaking 版本被关键下游拦住
        code, out, _ = self._run([
            "evaluate",
            str(EXAMPLES / "user_v1.json"),
            str(EXAMPLES / "user_v2_breaking.json"),
            "--consumers", str(EXAMPLES / "consumers"),
            "--format", "json",
        ])
        self.assertEqual(code, EXIT_INCOMPATIBLE)
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "blocked")
        consumers = {c["name"]: c for c in payload["consumers"]}
        self.assertFalse(consumers["payment-service"]["compatible"])
        self.assertTrue(consumers["analytics"]["compatible"])

    def test_example_consumers_directory_compatible(self):
        # 兼容版本：只有 strict 的非关键下游被新增字段卡住 → 放行但带警告
        code, out, _ = self._run([
            "evaluate",
            str(EXAMPLES / "user_v1.json"),
            str(EXAMPLES / "user_v2_compatible.json"),
            "--consumers", str(EXAMPLES / "consumers"),
            "--format", "json",
        ])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "pass_with_warnings")
        consumers = {c["name"]: c for c in payload["consumers"]}
        self.assertTrue(consumers["payment-service"]["compatible"])
        self.assertFalse(consumers["audit-log"]["compatible"])


if __name__ == "__main__":
    unittest.main()
