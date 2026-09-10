"""结构化差异计算测试。"""

from __future__ import annotations

import unittest

from datacontract.diff import ChangeKind, diff_contracts
from datacontract.parser import parse_dict


def _kinds(changes):
    return [(c.kind, c.path) for c in changes]


def _by_path(changes):
    return {c.path: c for c in changes}


class ScalarDiffTest(unittest.TestCase):
    def test_scalar_type_change(self):
        old = parse_dict({"version": "1", "root": "integer"})
        new = parse_dict({"version": "2", "root": "string"})
        changes = diff_contracts(old, new)
        self.assertEqual(_kinds(changes), [(ChangeKind.TYPE_CHANGED, "$")])
        self.assertEqual(changes[0].old, "integer")
        self.assertEqual(changes[0].new, "string")

    def test_enum_extend_reduce_replace(self):
        def c(old_enum, new_enum):
            old = parse_dict({"root": {"type": "string", "enum": old_enum}}) if old_enum else parse_dict({"root": "string"})
            new = parse_dict({"root": {"type": "string", "enum": new_enum}}) if new_enum else parse_dict({"root": "string"})
            return diff_contracts(old, new)[0].kind

        self.assertEqual(c(["A", "B"], ["A", "B", "C"]), ChangeKind.ENUM_EXTENDED)
        self.assertEqual(c(["A", "B", "C"], ["A", "B"]), ChangeKind.ENUM_REDUCED)
        self.assertEqual(c(["A", "B"], ["A", "C"]), ChangeKind.ENUM_REPLACED)
        self.assertEqual(c(None, ["A", "B"]), ChangeKind.ENUM_CONSTRAINED)
        self.assertEqual(c(["A", "B"], None), ChangeKind.ENUM_UNCONSTRAINED)

    def test_default_lifecycle(self):
        old = parse_dict({"root": {"type": "integer", "default": 1}})
        mid = parse_dict({"root": {"type": "integer", "default": 2}})
        new = parse_dict({"root": "integer"})

        (change,) = diff_contracts(old, mid)
        self.assertEqual(change.kind, ChangeKind.DEFAULT_CHANGED)
        self.assertEqual(change.old, 1)
        self.assertEqual(change.new, 2)

        (change,) = diff_contracts(old, new)
        self.assertEqual(change.kind, ChangeKind.DEFAULT_REMOVED)

        (change,) = diff_contracts(new, old)
        self.assertEqual(change.kind, ChangeKind.DEFAULT_ADDED)

    def test_no_changes(self):
        spec = {"root": {"type": "string", "enum": ["A"], "default": "A"}}
        self.assertEqual(diff_contracts(parse_dict(spec), parse_dict(spec)), [])


class ObjectDiffTest(unittest.TestCase):
    def _contract(self, fields, **kwargs):
        return parse_dict({"root": {"type": "object", "fields": fields, **kwargs}})

    def test_field_added_carries_meta(self):
        old = self._contract([{"name": "a", "type": "string"}])
        new = self._contract([
            {"name": "a", "type": "string"},
            {"name": "b", "type": "integer", "optional": True, "default": 0},
        ])
        (change,) = diff_contracts(old, new)
        self.assertEqual(change.kind, ChangeKind.FIELD_ADDED)
        self.assertEqual(change.path, "$.b")
        self.assertEqual(change.field, "b")
        self.assertTrue(change.meta["optional"])
        self.assertTrue(change.meta["has_default"])

    def test_required_field_added_meta(self):
        old = self._contract([])
        new = self._contract([{"name": "b", "type": "integer"}])
        (change,) = diff_contracts(old, new)
        self.assertFalse(change.meta["optional"])
        self.assertFalse(change.meta["has_default"])

    def test_field_removed(self):
        old = self._contract([{"name": "a", "type": "string"}])
        new = self._contract([])
        (change,) = diff_contracts(old, new)
        self.assertEqual(change.kind, ChangeKind.FIELD_REMOVED)
        self.assertEqual(change.path, "$.a")

    def test_optionality(self):
        old = self._contract([{"name": "a", "type": "string", "optional": True}])
        new = self._contract([{"name": "a", "type": "string", "optional": False}])
        (change,) = diff_contracts(old, new)
        self.assertEqual(change.kind, ChangeKind.FIELD_MADE_REQUIRED)
        (change,) = diff_contracts(new, old)
        self.assertEqual(change.kind, ChangeKind.FIELD_MADE_OPTIONAL)

    def test_nested_path(self):
        old = self._contract([{
            "name": "addr",
            "type": {"type": "object", "fields": [{"name": "city", "type": "string"}]},
        }])
        new = self._contract([{
            "name": "addr",
            "type": {"type": "object", "fields": [
                {"name": "city", "type": "string"},
                {"name": "zip", "type": "string"},
            ]},
        }])
        changes = diff_contracts(old, new)
        self.assertIn((ChangeKind.FIELD_ADDED, "$.addr.zip"), _kinds(changes))

    def test_additional_properties_change(self):
        old = self._contract([], additionalProperties=False)
        new = self._contract([], additionalProperties=True)
        (change,) = diff_contracts(old, new)
        self.assertEqual(change.kind, ChangeKind.ADDITIONAL_PROPERTIES_CHANGED)


class ArrayDiffTest(unittest.TestCase):
    def test_array_element_change(self):
        old = parse_dict({"root": {"type": "array", "items": "integer"}})
        new = parse_dict({"root": {"type": "array", "items": "string"}})
        (change,) = diff_contracts(old, new)
        self.assertEqual(change.kind, ChangeKind.TYPE_CHANGED)
        self.assertEqual(change.path, "$[]")

    def test_nested_array_of_objects_path(self):
        spec_old = {"root": {"type": "array", "items": {
            "type": "object", "fields": [{"name": "id", "type": "integer"}]}}}
        spec_new = {"root": {"type": "array", "items": {
            "type": "object", "fields": [{"name": "id", "type": "string"}]}}}
        (change,) = diff_contracts(parse_dict(spec_old), parse_dict(spec_new))
        self.assertEqual(change.path, "$[].id")


class RefDiffTest(unittest.TestCase):
    def test_change_through_ref_uses_data_path(self):
        old = parse_dict({
            "defs": {"Addr": {"type": "object", "fields": [
                {"name": "city", "type": "string"}]}},
            "root": {"type": "object", "fields": [
                {"name": "address", "type": "Addr"}]},
        })
        new = parse_dict({
            "defs": {"Addr": {"type": "object", "fields": [
                {"name": "city", "type": "integer"}]}},
            "root": {"type": "object", "fields": [
                {"name": "address", "type": "Addr"}]},
        })
        changes = diff_contracts(old, new)
        # 数据路径下的变化必须出现（另有 $defs.Addr.city 会被去重）
        paths = [c.path for c in changes]
        self.assertIn("$.address.city", paths)
        data_changes = [c for c in changes if not c.path.startswith("$defs.")]
        self.assertEqual(len(data_changes), 1)

    def test_recursive_structure_does_not_loop(self):
        def contract(id_type):
            return parse_dict({
                "defs": {"Node": {"type": "object", "fields": [
                    {"name": "id", "type": id_type},
                    {"name": "children", "type": {"type": "array", "items": "Node"}},
                ]}},
                "root": "Node",
            })
        changes = diff_contracts(contract("integer"), contract("string"))
        self.assertTrue(any(c.kind is ChangeKind.TYPE_CHANGED for c in changes))

    def test_def_added_and_removed(self):
        old = parse_dict({"defs": {"A": {"type": "string"}}, "root": "string"})
        new = parse_dict({"defs": {"B": {"type": "string"}}, "root": "string"})
        kinds = {c.kind for c in diff_contracts(old, new)}
        self.assertIn(ChangeKind.FIELD_ADDED, kinds)
        self.assertIn(ChangeKind.FIELD_REMOVED, kinds)


if __name__ == "__main__":
    unittest.main()
