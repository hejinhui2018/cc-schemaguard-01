"""多下游治理评估测试。"""

from __future__ import annotations

import json
import unittest

from datacontract.consumer import ConsumerError, ConsumerProfile
from datacontract.governance import (
    FAIL_ANY,
    FAIL_CRITICAL,
    FAIL_NEVER,
    GateVerdict,
    evaluate_governance,
)
from datacontract.migration import PHASE_PRODUCER_CONTRACT
from datacontract.parser import parse_dict


def _contract(fields, version="1", defs=None):
    spec = {"version": version, "root": {"type": "object", "fields": fields}}
    if defs:
        spec["defs"] = defs
    return parse_dict(spec)


def _consumer(name, fields, *, critical=False, strict=False):
    return ConsumerProfile(
        name=name,
        critical=critical,
        strict=strict,
        contract=parse_dict({"root": {"type": "object", "fields": fields}}),
    )


# 上游老契约：id(必填 int) / name(必填 str) / status(枚举) / nick(可选)
OLD_FIELDS = [
    {"name": "id", "type": "integer"},
    {"name": "name", "type": "string"},
    {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
    {"name": "nick", "type": "string", "optional": True},
]
OLD = _contract(OLD_FIELDS)

C_ID = _consumer("c-id", [{"name": "id", "type": "integer"}])
C_NAME = _consumer("c-name", [{"name": "name", "type": "string"}])
C_STATUS = _consumer(
    "c-status",
    [{"name": "status", "type": {"type": "string", "enum": ["A", "B"]}}],
)


def _evaluate(new_fields, consumers, **kwargs):
    new = _contract(new_fields, version="2")
    return evaluate_governance(OLD, new, consumers, **kwargs)


class ProjectionTest(unittest.TestCase):
    """需求一：下游绑定的字段范围可以与上游不同。"""

    def test_field_added_out_of_scope_does_not_affect_lenient_consumer(self):
        # 上游新增可选字段：非 strict 下游完全无感（连警告都不投影进来）
        ev = _evaluate(OLD_FIELDS + [{"name": "extra", "type": "string", "optional": True}],
                       [C_ID, C_NAME])
        self.assertEqual(ev.report.verdict, GateVerdict.PASS)
        for a in ev.report.consumers:
            self.assertTrue(a.compatible)
            self.assertEqual(a.findings, ())

    def test_field_removed_out_of_scope_does_not_affect(self):
        # 上游删除 name：只读 id 的下游不受影响
        new_fields = [f for f in OLD_FIELDS if f["name"] != "name"]
        ev = _evaluate(new_fields, [C_ID])
        a = ev.report.consumers[0]
        self.assertTrue(a.compatible)
        self.assertEqual(a.relevant_changes, ())

    def test_field_removed_in_scope_is_found(self):
        new_fields = [f for f in OLD_FIELDS if f["name"] != "name"]
        ev = _evaluate(new_fields, [C_NAME])
        a = ev.report.consumers[0]
        self.assertFalse(a.compatible)
        self.assertEqual({f.rule_id for f in a.errors}, {"F002"})
        self.assertEqual(a.errors[0].path, "$.name")

    def test_type_change_only_hits_consumers_of_that_field(self):
        new_fields = [
            {"name": "id", "type": "string"},
            {"name": "name", "type": "string"},
            {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
            {"name": "nick", "type": "string", "optional": True},
        ]
        ev = _evaluate(new_fields, [C_ID, C_NAME])
        by_name = {a.name: a for a in ev.report.consumers}
        self.assertFalse(by_name["c-id"].compatible)
        self.assertEqual({f.rule_id for f in by_name["c-id"].errors}, {"F021"})
        self.assertTrue(by_name["c-name"].compatible)

    def test_enum_extension_hits_only_status_consumers(self):
        new_fields = [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "string"},
            {"name": "status", "type": {"type": "string", "enum": ["A", "B", "C"]}},
            {"name": "nick", "type": "string", "optional": True},
        ]
        ev = _evaluate(new_fields, [C_ID, C_STATUS])
        by_name = {a.name: a for a in ev.report.consumers}
        self.assertTrue(by_name["c-id"].compatible)
        self.assertEqual({f.rule_id for f in by_name["c-status"].errors}, {"F010"})

    def test_benign_change_is_projected_but_no_finding(self):
        # nick 可选→必填：对老消费者良性（F003 不产生 finding），但仍计入 relevant_changes
        new_fields = [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "string"},
            {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
            {"name": "nick", "type": "string"},
        ]
        c_nick = _consumer("c-nick", [{"name": "nick", "type": "string", "optional": True}])
        ev = _evaluate(new_fields, [c_nick])
        a = ev.report.consumers[0]
        self.assertTrue(a.compatible)
        self.assertEqual([c.kind.value for c in a.relevant_changes], ["field_made_required"])

    def test_unreferenced_defs_change_not_projected(self):
        old = parse_dict({
            "version": "1",
            "defs": {"Unused": {"type": "object", "fields": [
                {"name": "x", "type": "string"}]}},
            "root": {"type": "object", "fields": [{"name": "id", "type": "integer"}]},
        })
        new = parse_dict({
            "version": "2",
            "defs": {"Unused": {"type": "object", "fields": [
                {"name": "x", "type": "integer"}]}},
            "root": {"type": "object", "fields": [{"name": "id", "type": "integer"}]},
        })
        ev = evaluate_governance(old, new, [C_ID])
        self.assertEqual(ev.report.verdict, GateVerdict.PASS)
        self.assertEqual(ev.report.consumers[0].findings, ())


class NestedAndArrayProjectionTest(unittest.TestCase):
    def test_nested_sibling_change_not_affecting(self):
        old = _contract([
            {"name": "address", "type": {"type": "object", "fields": [
                {"name": "city", "type": "string"},
                {"name": "zip", "type": "string"},
            ]}},
        ])
        new = _contract([
            {"name": "address", "type": {"type": "object", "fields": [
                {"name": "city", "type": "string"},
            ]}},
        ], version="2")
        c_city = _consumer("c-city", [
            {"name": "address", "type": {"type": "object", "fields": [
                {"name": "city", "type": "string"}]}},
        ])
        c_zip = _consumer("c-zip", [
            {"name": "address", "type": {"type": "object", "fields": [
                {"name": "zip", "type": "string"}]}},
        ])
        ev = evaluate_governance(old, new, [c_city, c_zip])
        by_name = {a.name: a for a in ev.report.consumers}
        self.assertTrue(by_name["c-city"].compatible)
        self.assertFalse(by_name["c-zip"].compatible)
        self.assertEqual(by_name["c-zip"].errors[0].path, "$.address.zip")

    def test_array_element_projection(self):
        old = parse_dict({"version": "1", "root": {"type": "object", "fields": [
            {"name": "items", "type": {"type": "array", "items": {
                "type": "object", "fields": [
                    {"name": "id", "type": "integer"},
                    {"name": "kind", "type": "string"},
                ]}}}]}})
        new = parse_dict({"version": "2", "root": {"type": "object", "fields": [
            {"name": "items", "type": {"type": "array", "items": {
                "type": "object", "fields": [
                    {"name": "id", "type": "string"},
                    {"name": "kind", "type": "string"},
                ]}}}]}})
        c_item_id = _consumer("c-item-id", [
            {"name": "items", "type": {"type": "array", "items": {
                "type": "object", "fields": [{"name": "id", "type": "integer"}]}}},
        ])
        c_item_kind = _consumer("c-item-kind", [
            {"name": "items", "type": {"type": "array", "items": {
                "type": "object", "fields": [{"name": "kind", "type": "string"}]}}},
        ])
        ev = evaluate_governance(old, new, [c_item_id, c_item_kind])
        by_name = {a.name: a for a in ev.report.consumers}
        self.assertFalse(by_name["c-item-id"].compatible)
        self.assertEqual(by_name["c-item-id"].errors[0].path, "$.items[].id")
        self.assertTrue(by_name["c-item-kind"].compatible)


class StrictConsumerTest(unittest.TestCase):
    def test_strict_consumer_breaks_on_added_field(self):
        c_strict = _consumer("c-strict", [
            {"name": "id", "type": "integer"},
        ], strict=True)
        ev = _evaluate(
            OLD_FIELDS + [{"name": "extra", "type": "string", "optional": True}],
            [C_ID, c_strict],
        )
        by_name = {a.name: a for a in ev.report.consumers}
        self.assertTrue(by_name["c-id"].compatible)
        strict_a = by_name["c-strict"]
        self.assertFalse(strict_a.compatible)
        self.assertEqual({f.rule_id for f in strict_a.errors}, {"F001"})
        self.assertIn("严格解析", strict_a.errors[0].message)

    def test_strict_consumer_ignores_addition_outside_its_objects(self):
        # 新增发生在下游不解析的子对象里：strict 也不受影响
        old = _contract([
            {"name": "id", "type": "integer"},
            {"name": "address", "type": {"type": "object", "fields": [
                {"name": "city", "type": "string"}]}},
        ])
        new = _contract([
            {"name": "id", "type": "integer"},
            {"name": "address", "type": {"type": "object", "fields": [
                {"name": "city", "type": "string"},
                {"name": "zip", "type": "string", "optional": True},
            ]}},
        ], version="2")
        c_strict = _consumer("c-strict", [{"name": "id", "type": "integer"}], strict=True)
        ev = evaluate_governance(old, new, [c_strict])
        self.assertTrue(ev.report.consumers[0].compatible)


class VerdictTest(unittest.TestCase):
    """需求四：治理放行口径。"""

    # id integer→string：双向不兼容（下游 F021 + 上游自身 B021）
    BREAKING_ID = [
        {"name": "id", "type": "string"},
        {"name": "name", "type": "string"},
        {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
        {"name": "nick", "type": "string", "optional": True},
    ]
    # 删除必填 name：仅前向不兼容（下游 F002），上游自身后向只有警告
    NAME_REMOVED = [f for f in OLD_FIELDS if f["name"] != "name"]

    def test_critical_consumer_broken_blocks_release(self):
        c_critical = _consumer("pay", [{"name": "id", "type": "integer"}], critical=True)
        ev = _evaluate(self.BREAKING_ID, [c_critical, C_NAME])
        self.assertEqual(ev.report.verdict, GateVerdict.BLOCKED)
        self.assertFalse(ev.passed)
        self.assertEqual(ev.exit_code(), 1)

    def test_only_non_critical_broken_passes_with_warnings(self):
        ev = _evaluate(self.NAME_REMOVED, [C_ID, C_NAME])  # 都非关键，仅 c-name 受损
        self.assertEqual(ev.report.verdict, GateVerdict.PASS_WITH_WARNINGS)
        self.assertTrue(ev.passed)  # 默认 fail_on=critical
        self.assertEqual(ev.exit_code(), 0)

    def test_fail_on_any_turns_warning_into_block(self):
        ev = _evaluate(self.NAME_REMOVED, [C_ID, C_NAME], fail_on=FAIL_ANY)
        self.assertFalse(ev.passed)
        self.assertEqual(ev.exit_code(), 1)

    def test_fail_on_never_always_passes(self):
        c_critical = _consumer("pay", [{"name": "id", "type": "integer"}], critical=True)
        ev = _evaluate(self.BREAKING_ID, [c_critical], fail_on=FAIL_NEVER)
        self.assertTrue(ev.passed)
        self.assertEqual(ev.report.verdict, GateVerdict.BLOCKED)  # 口径本身仍如实给出

    def test_all_compatible_passes(self):
        ev = _evaluate(OLD_FIELDS + [{"name": "extra", "type": "string", "optional": True}],
                       [C_ID, C_NAME])
        self.assertEqual(ev.report.verdict, GateVerdict.PASS)
        self.assertTrue(ev.passed)

    def test_producer_backward_error_blocks_even_with_happy_consumers(self):
        # 上游新增必填无默认字段：下游无人消费它，但上游自身读历史数据会缺值
        ev = _evaluate(
            OLD_FIELDS + [{"name": "email", "type": "string"}],
            [C_ID, C_NAME],
        )
        self.assertEqual(ev.report.verdict, GateVerdict.BLOCKED)
        self.assertFalse(ev.passed)
        reasons = ev.report.blocking_reasons
        self.assertTrue(any("上游自身后向不兼容" in r and "B001" in r for r in reasons))

    def test_reasons_traceable_to_consumer_path_and_rule(self):
        c_critical = _consumer("pay", [{"name": "name", "type": "string"}], critical=True)
        ev = _evaluate(self.NAME_REMOVED, [c_critical, C_ID])
        reasons = ev.report.blocking_reasons
        self.assertEqual(len(reasons), 1)
        self.assertIn("pay", reasons[0])
        self.assertIn("$.name", reasons[0])
        self.assertIn("F002", reasons[0])

    def test_no_consumers_degrades_to_producer_only(self):
        # 零下游：前向不兼容只能提示「影响面未知」，后向不兼容仍阻断
        removed_name = [f for f in OLD_FIELDS if f["name"] != "name"]
        ev = _evaluate(removed_name, [])
        self.assertEqual(ev.report.verdict, GateVerdict.PASS_WITH_WARNINGS)
        self.assertTrue(any("未提供下游画像" in r for r in ev.report.warning_reasons))

        ev2 = _evaluate(OLD_FIELDS + [{"name": "email", "type": "string"}], [])
        self.assertEqual(ev2.report.verdict, GateVerdict.BLOCKED)

    def test_duplicate_consumer_names_rejected(self):
        with self.assertRaises(ConsumerError):
            _evaluate(OLD_FIELDS, [C_ID, ConsumerProfile(
                name="c-id", critical=False, strict=False,
                contract=parse_dict({"root": "string"}),
            )])


class GovernancePlanTest(unittest.TestCase):
    """需求三：迁移建议落到下游。"""

    def test_consumer_steps_carry_consumer_actor(self):
        new_fields = [f for f in OLD_FIELDS if f["name"] != "name"]
        ev = _evaluate(new_fields, [C_NAME])
        steps = ev.report.plan.steps_for("c-name")
        self.assertTrue(steps)
        self.assertTrue(all(s.actor == "consumer:c-name" for s in steps))
        self.assertTrue(any("c-name" in s.action for s in steps))

    def test_producer_steps_merged_across_consumers(self):
        # 两个下游都因 name 删除受影响：上游的「标记废弃但继续下发」只出现一次
        new_fields = [f for f in OLD_FIELDS if f["name"] != "name"]
        c_name2 = _consumer("c-name-2", [{"name": "name", "type": "string"}])
        ev = _evaluate(new_fields, [C_NAME, c_name2])
        expand = [
            s for s in ev.report.plan.producer_steps
            if "继续下发" in s.action and s.paths == ("$.name",)
        ]
        self.assertEqual(len(expand), 1)
        self.assertIn("c-name", expand[0].detail)
        self.assertIn("c-name-2", expand[0].detail)
        # 但两个下游各自的消费侧步骤独立存在
        self.assertTrue(ev.report.plan.steps_for("c-name"))
        self.assertTrue(ev.report.plan.steps_for("c-name-2"))

    def test_contract_phase_step_waits_for_all_consumers(self):
        new_fields = [f for f in OLD_FIELDS if f["name"] != "name"]
        c_name2 = _consumer("c-name-2", [{"name": "name", "type": "string"}])
        ev = _evaluate(new_fields, [C_NAME, c_name2])
        contract_steps = [
            s for s in ev.report.plan.producer_steps
            if s.phase == PHASE_PRODUCER_CONTRACT and s.paths == ("$.name",)
        ]
        self.assertEqual(len(contract_steps), 1)
        step = contract_steps[0]
        self.assertFalse(step.rollback_safe)
        self.assertIn("c-name", step.detail)
        self.assertIn("c-name-2", step.detail)
        self.assertIn("全部完成迁移", step.detail)

    def test_rollback_flags_preserved(self):
        new_fields = [f for f in OLD_FIELDS if f["name"] != "name"]
        ev = _evaluate(new_fields, [C_NAME])
        error_steps = [s for s in ev.report.plan.steps if s.severity == "error"]
        self.assertTrue(error_steps)
        # expand/consumer 阶段可回滚，contract 阶段不可
        for s in error_steps:
            if s.phase == PHASE_PRODUCER_CONTRACT:
                self.assertFalse(s.rollback_safe)
            else:
                self.assertTrue(s.rollback_safe)
            self.assertTrue(s.rollback)  # 每步都有回滚说明

    def test_type_change_merges_forward_and_backward_into_one_plan(self):
        # id integer→string：下游 F021 + 上游自身 B021 是同一物理变更，
        # producer 步骤只应出现一套，规则号合并为 F021/B021
        ev = _evaluate(self.BREAKING_ID_FIELDS, [C_ID])
        producer_id_steps = [
            s for s in ev.report.plan.producer_steps if s.paths == ("$.id",)
        ]
        self.assertTrue(producer_id_steps)
        self.assertTrue(all(s.rule_id == "F021/B021" for s in producer_id_steps))
        actions = [s.action for s in producer_id_steps]
        self.assertEqual(len(actions), len(set(actions)), "producer 步骤不应重复")
        self.assertTrue(any("双写" in a for a in actions))

    BREAKING_ID_FIELDS = [
        {"name": "id", "type": "string"},
        {"name": "name", "type": "string"},
        {"name": "status", "type": {"type": "string", "enum": ["A", "B"]}},
        {"name": "nick", "type": "string", "optional": True},
    ]

    def test_steps_sorted_and_sequentially_numbered(self):
        ev = _evaluate(self.BREAKING_ID_FIELDS, [C_ID, C_STATUS])
        orders = [s.order for s in ev.report.plan.steps]
        self.assertEqual(orders, list(range(1, len(orders) + 1)))
        phases = [s.phase for s in ev.report.plan.steps]
        self.assertEqual(phases, sorted(phases))

    def test_compatible_release_has_no_error_steps(self):
        ev = _evaluate(OLD_FIELDS + [{"name": "extra", "type": "string", "optional": True}],
                       [C_ID])
        self.assertFalse(ev.report.plan.blocking)
        self.assertEqual(
            [s for s in ev.report.plan.steps if s.severity == "error"], []
        )


class SerializationTest(unittest.TestCase):
    def test_to_dict_is_json_serializable(self):
        c_critical = _consumer("pay", [{"name": "id", "type": "integer"}], critical=True)
        ev = _evaluate(VerdictTest.BREAKING_ID, [c_critical, C_NAME])
        text = json.dumps(ev.to_dict(), ensure_ascii=False)
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], "blocked")
        self.assertFalse(payload["passed"])
        self.assertEqual(payload["fail_on"], FAIL_CRITICAL)
        consumers = {c["name"]: c for c in payload["consumers"]}
        self.assertFalse(consumers["pay"]["compatible"])
        self.assertTrue(consumers["c-name"]["compatible"])
        self.assertTrue(payload["blocking_reasons"])
        self.assertIn("plan", payload)
        self.assertIn("producer", payload)


if __name__ == "__main__":
    unittest.main()
