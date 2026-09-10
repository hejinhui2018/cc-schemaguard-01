"""迁移规划测试。"""

from __future__ import annotations

import unittest

from datacontract.compatibility import check_compatibility
from datacontract.diff import diff_contracts
from datacontract.migration import (
    PHASE_BACKFILL,
    PHASE_CONSUMER_UPGRADE,
    PHASE_PRODUCER_CONTRACT,
    PHASE_PRODUCER_EXPAND,
    plan_migration,
)
from datacontract.parser import parse_dict


def _plan(old_spec, new_spec):
    old = parse_dict(old_spec)
    new = parse_dict(new_spec)
    changes = diff_contracts(old, new)
    report = check_compatibility(old, new, changes=changes)
    return report, plan_migration(report)


class MigrationPlanTest(unittest.TestCase):
    def test_compatible_change_yields_only_advisories(self):
        old = {"version": "1", "root": {"type": "object", "fields": [
            {"name": "a", "type": "string"}]}}
        new = {"version": "2", "root": {"type": "object", "fields": [
            {"name": "a", "type": "string"},
            {"name": "b", "type": "string", "optional": True}]}}
        report, plan = _plan(old, new)
        self.assertTrue(report.fully_compatible)
        self.assertFalse(plan.blocking)
        self.assertTrue(all(s.severity == "warning" for s in plan.steps))

    def test_required_field_added_plan_order_and_actors(self):
        old = {"version": "1", "root": {"type": "object", "fields": []}}
        new = {"version": "2", "root": {"type": "object", "fields": [
            {"name": "b", "type": "string"}]}}
        report, plan = _plan(old, new)
        self.assertFalse(report.backward_compatible)
        self.assertTrue(plan.blocking)

        phases = [s.phase for s in plan.steps]
        self.assertEqual(
            phases,
            sorted(phases),
            "步骤必须按阶段有序排列",
        )
        # expand → consumer → backfill → contract 四步齐全
        self.assertIn(PHASE_PRODUCER_EXPAND, phases)
        self.assertIn(PHASE_CONSUMER_UPGRADE, phases)
        self.assertIn(PHASE_BACKFILL, phases)

        error_steps = [s for s in plan.steps if s.severity == "error"]
        error_phases = [s.phase for s in error_steps]
        # 最后一个阻断步骤必须是“收窄”阶段
        self.assertEqual(error_phases[-1], PHASE_PRODUCER_CONTRACT)

        # 收窄步骤必须标注不可安全回滚
        last = error_steps[-1]
        self.assertFalse(last.rollback_safe)
        self.assertTrue(last.rollback)
        # 早期加法步骤可回滚
        self.assertTrue(all(s.rollback_safe for s in error_steps[:-1]))

        # 每一步都能追溯规则与路径
        for s in error_steps:
            self.assertEqual(s.rule_id, "B001")
            self.assertEqual(s.paths, ("$.b",))
            self.assertIn(s.actor, ("producer", "consumer", "both"))
            self.assertTrue(s.action and s.detail)

    def test_enum_extension_consumers_upgrade_before_producer_emits(self):
        old = {"version": "1", "root": {"type": "string", "enum": ["A"]}}
        new = {"version": "2", "root": {"type": "string", "enum": ["A", "B"]}}
        report, plan = _plan(old, new)
        errors = [s for s in plan.steps if s.severity == "error"]
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[0].phase, PHASE_CONSUMER_UPGRADE)
        self.assertEqual(errors[1].phase, PHASE_PRODUCER_CONTRACT)
        self.assertFalse(errors[1].rollback_safe)

    def test_field_removed_keeps_emitting_during_window(self):
        old = {"version": "1", "root": {"type": "object", "fields": [
            {"name": "a", "type": "string"}]}}
        new = {"version": "2", "root": {"type": "object", "fields": []}}
        _, plan = _plan(old, new)
        errors = [s for s in plan.steps if s.severity == "error"]
        # 第一步是“标记废弃但继续下发”，最后才是停发
        self.assertIn("继续下发", errors[0].action + errors[0].detail)
        self.assertEqual(errors[-1].phase, PHASE_PRODUCER_CONTRACT)
        self.assertFalse(errors[-1].rollback_safe)

    def test_order_numbers_are_sequential(self):
        old = {"version": "1", "root": {"type": "object", "fields": []}}
        new = {"version": "2", "root": {"type": "object", "fields": [
            {"name": "b", "type": "string"},
            {"name": "c", "type": {"type": "string", "enum": ["X", "Y"]}}]}}
        # 让 b 必填无默认 + c 枚举扩展（new 有枚举，old 无 → constrained 只伤后向）
        old2 = {"version": "1", "root": {"type": "object", "fields": [
            {"name": "c", "type": {"type": "string", "enum": ["X"]}}]}}
        _, plan = _plan(old2, new)
        self.assertEqual([s.order for s in plan.steps], list(range(1, len(plan.steps) + 1)))

    def test_plan_dict_shape(self):
        old = {"version": "1", "root": "string"}
        new = {"version": "2", "root": "integer"}
        _, plan = _plan(old, new)
        payload = plan.to_dict()
        self.assertTrue(payload["blocking"])
        self.assertGreaterEqual(len(payload["steps"]), 2)
        row = payload["steps"][0]
        self.assertEqual(
            set(row),
            {"order", "phase", "actor", "action", "detail",
             "rollback_safe", "rollback", "rule_id", "paths", "severity"},
        )


if __name__ == "__main__":
    unittest.main()
