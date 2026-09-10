"""兼容性判定规则矩阵测试。"""

from __future__ import annotations

import unittest

from datacontract.compatibility import Direction, Severity, check_compatibility
from datacontract.diff import diff_contracts
from datacontract.parser import parse_dict


def _check(old_spec, new_spec):
    old = parse_dict(old_spec)
    new = parse_dict(new_spec)
    return old, new, check_compatibility(old, new, changes=diff_contracts(old, new))


def _obj(fields_old, fields_new=None):
    old = {"version": "1", "root": {"type": "object", "fields": fields_old}}
    if fields_new is None:
        fields_new = fields_old
    new = {"version": "2", "root": {"type": "object", "fields": fields_new}}
    return old, new


def _rule_ids(findings):
    return {f.rule_id for f in findings}


class FieldChangeTest(unittest.TestCase):
    def test_optional_field_added_is_fully_compatible(self):
        old, new = _obj(
            [{"name": "a", "type": "string"}],
            [
                {"name": "a", "type": "string"},
                {"name": "b", "type": "string", "optional": True},
            ],
        )
        _, _, report = _check(old, new)
        self.assertTrue(report.fully_compatible)
        # 前向上仍给一条“严格解析器注意”的警告
        self.assertIn("F001", _rule_ids(report.warnings(Direction.FORWARD)))

    def test_required_field_with_default_added_is_backward_safe(self):
        old, new = _obj(
            [{"name": "a", "type": "string"}],
            [
                {"name": "a", "type": "string"},
                {"name": "b", "type": "integer", "default": 0},
            ],
        )
        _, _, report = _check(old, new)
        self.assertTrue(report.backward_compatible)
        self.assertNotIn("B001", _rule_ids(report.errors(Direction.BACKWARD)))

    def test_required_field_added_without_default_breaks_backward(self):
        old, new = _obj(
            [],
            [{"name": "b", "type": "string"}],
        )
        _, _, report = _check(old, new)
        self.assertFalse(report.backward_compatible)
        self.assertTrue(report.forward_compatible)
        errors = report.errors(Direction.BACKWARD)
        self.assertEqual({f.rule_id for f in errors}, {"B001"})
        self.assertEqual(errors[0].path, "$.b")
        # 结论必须能追溯到具体变更
        self.assertEqual(errors[0].changes[0].field, "b")

    def test_required_field_removed_breaks_forward(self):
        old, new = _obj(
            [{"name": "a", "type": "string"}],
            [],
        )
        _, _, report = _check(old, new)
        self.assertFalse(report.forward_compatible)
        self.assertTrue(report.backward_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.FORWARD)), {"F002"})

    def test_optional_field_removed_is_forward_safe(self):
        old, new = _obj(
            [{"name": "a", "type": "string", "optional": True}],
            [],
        )
        _, _, report = _check(old, new)
        self.assertTrue(report.forward_compatible)

    def test_required_to_optional_breaks_forward(self):
        old, new = _obj(
            [{"name": "a", "type": "string"}],
            [{"name": "a", "type": "string", "optional": True}],
        )
        _, _, report = _check(old, new)
        self.assertFalse(report.forward_compatible)
        self.assertTrue(report.backward_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.FORWARD)), {"F004"})

    def test_optional_to_required_breaks_backward(self):
        old, new = _obj(
            [{"name": "a", "type": "string", "optional": True}],
            [{"name": "a", "type": "string"}],
        )
        _, _, report = _check(old, new)
        self.assertTrue(report.forward_compatible)
        self.assertFalse(report.backward_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.BACKWARD)), {"B003"})


class EnumTest(unittest.TestCase):
    def _pair(self, old_enum, new_enum):
        old = {"version": "1", "root": (
            {"type": "string", "enum": old_enum} if old_enum is not None else {"type": "string"})}
        new = {"version": "2", "root": (
            {"type": "string", "enum": new_enum} if new_enum is not None else {"type": "string"})}
        return old, new

    def test_enum_extended_breaks_forward_only(self):
        old, new = self._pair(["A", "B"], ["A", "B", "C"])
        _, _, report = _check(old, new)
        self.assertFalse(report.forward_compatible)
        self.assertTrue(report.backward_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.FORWARD)), {"F010"})

    def test_enum_reduced_breaks_backward_only(self):
        old, new = self._pair(["A", "B", "C"], ["A", "B"])
        _, _, report = _check(old, new)
        self.assertTrue(report.forward_compatible)
        self.assertFalse(report.backward_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.BACKWARD)), {"B011"})

    def test_enum_replaced_breaks_both(self):
        old, new = self._pair(["A", "B"], ["A", "C"])
        _, _, report = _check(old, new)
        self.assertFalse(report.forward_compatible)
        self.assertFalse(report.backward_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.FORWARD)), {"F012"})
        self.assertEqual(_rule_ids(report.errors(Direction.BACKWARD)), {"B012"})

    def test_enum_constrained_breaks_backward(self):
        old, new = self._pair(None, ["A", "B"])
        _, _, report = _check(old, new)
        self.assertTrue(report.forward_compatible)
        self.assertFalse(report.backward_compatible)

    def test_enum_unconstrained_breaks_forward(self):
        old, new = self._pair(["A", "B"], None)
        _, _, report = _check(old, new)
        self.assertFalse(report.forward_compatible)
        self.assertTrue(report.backward_compatible)


class TypeChangeTest(unittest.TestCase):
    def test_unrelated_scalar_change_breaks_both(self):
        old = {"version": "1", "root": "string"}
        new = {"version": "2", "root": "boolean"}
        _, _, report = _check(old, new)
        self.assertFalse(report.fully_compatible)
        self.assertEqual(_rule_ids(report.errors(Direction.FORWARD)), {"F021"})
        self.assertEqual(_rule_ids(report.errors(Direction.BACKWARD)), {"B021"})

    def test_integer_to_number_is_warning_forward_safe_backward(self):
        old = {"version": "1", "root": "integer"}
        new = {"version": "2", "root": "number"}
        _, _, report = _check(old, new)
        self.assertTrue(report.fully_compatible)  # 只有警告
        fwd = report.findings(Direction.FORWARD)
        self.assertEqual(len(fwd), 1)
        self.assertIs(fwd[0].severity, Severity.WARNING)
        self.assertEqual(fwd[0].rule_id, "F020")
        self.assertEqual(report.findings(Direction.BACKWARD), [])

    def test_number_to_integer_is_safe_forward_warning_backward(self):
        old = {"version": "1", "root": "number"}
        new = {"version": "2", "root": "integer"}
        _, _, report = _check(old, new)
        self.assertTrue(report.fully_compatible)
        self.assertEqual(report.findings(Direction.FORWARD), [])
        bwd = report.findings(Direction.BACKWARD)
        self.assertEqual(bwd[0].rule_id, "B020")
        self.assertIs(bwd[0].severity, Severity.WARNING)

    def test_form_change_object_to_array(self):
        old = {"version": "1", "root": {"type": "object", "fields": []}}
        new = {"version": "2", "root": {"type": "array", "items": "string"}}
        _, _, report = _check(old, new)
        self.assertFalse(report.fully_compatible)


class DefaultTest(unittest.TestCase):
    def test_default_change_is_warning_both_directions_but_compatible(self):
        old = {"version": "1", "root": {"type": "integer", "default": 1}}
        new = {"version": "2", "root": {"type": "integer", "default": 2}}
        _, _, report = _check(old, new)
        self.assertTrue(report.fully_compatible)
        self.assertEqual(_rule_ids(report.warnings(Direction.FORWARD)), {"F032"})
        self.assertEqual(_rule_ids(report.warnings(Direction.BACKWARD)), {"B032"})

    def test_default_added_and_removed(self):
        with_d = {"version": "1", "root": {"type": "integer", "default": 1}}
        without_d = {"version": "2", "root": "integer"}
        _, _, r1 = _check(without_d, with_d)
        _, _, r2 = _check(with_d, without_d)
        self.assertTrue(r1.fully_compatible and r2.fully_compatible)
        self.assertIn("F030", _rule_ids(r1.warnings()))
        self.assertIn("F031", _rule_ids(r2.warnings()))


class NestedAndRefTest(unittest.TestCase):
    def test_nested_array_object_breaking_change_traces_path(self):
        old = {"version": "1", "root": {"type": "array", "items": {
            "type": "object", "fields": [
                {"name": "id", "type": "integer"},
                {"name": "kind", "type": {"type": "string", "enum": ["X", "Y"]}},
            ]}}}
        new = {"version": "2", "root": {"type": "array", "items": {
            "type": "object", "fields": [
                {"name": "id", "type": "integer"},
                {"name": "kind", "type": {"type": "string", "enum": ["X", "Y", "Z"]}},
            ]}}}
        _, _, report = _check(old, new)
        errors = report.errors(Direction.FORWARD)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].path, "$[].kind")

    def test_change_through_ref_attributed_at_data_path(self):
        def contract(city_type):
            return {
                "version": "1",
                "defs": {"Addr": {"type": "object", "fields": [
                    {"name": "city", "type": city_type}]}},
                "root": {"type": "object", "fields": [
                    {"name": "address", "type": "Addr"}]},
            }
        _, _, report = _check(contract("string"), contract("integer"))
        fwd = report.errors(Direction.FORWARD)
        self.assertEqual([f.path for f in fwd], ["$.address.city"])

    def test_shared_def_change_reported_at_each_use_site(self):
        def contract(city_type):
            return {
                "version": "1",
                "defs": {"Addr": {"type": "object", "fields": [
                    {"name": "city", "type": city_type}]}},
                "root": {"type": "object", "fields": [
                    {"name": "home", "type": "Addr"},
                    {"name": "work", "type": "Addr"},
                ]},
            }
        _, _, report = _check(contract("string"), contract("integer"))
        paths = sorted(f.path for f in report.errors(Direction.FORWARD))
        self.assertEqual(paths, ["$.home.city", "$.work.city"])

    def test_recursive_structure_compatibility(self):
        def contract(id_type):
            return {
                "version": "1",
                "defs": {"Node": {"type": "object", "fields": [
                    {"name": "id", "type": id_type},
                    {"name": "children", "type": {"type": "array", "items": "Node"}},
                ]}},
                "root": "Node",
            }
        _, _, report = _check(contract("integer"), contract("string"))
        self.assertFalse(report.fully_compatible)
        self.assertTrue(all("id" in f.path for f in report.errors()))


class AggregateTest(unittest.TestCase):
    def test_identical_contracts_fully_compatible_no_findings(self):
        spec = {
            "version": "1",
            "root": {"type": "object", "fields": [
                {"name": "a", "type": "string", "optional": True}]},
        }
        _, _, report = _check(spec, spec)
        self.assertTrue(report.fully_compatible)
        self.assertEqual(report.changes, [])
        self.assertEqual(report.errors(), [])
        self.assertEqual(report.warnings(), [])

    def test_to_dict_is_json_serializable(self):
        import json

        old = {"version": "1", "root": "integer"}
        new = {"version": "2", "root": "string"}
        _, _, report = _check(old, new)
        payload = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertIn('"fully_compatible": false', payload)


if __name__ == "__main__":
    unittest.main()
