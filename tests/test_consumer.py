"""下游消费画像与消费点投影测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from datacontract.consumer import (
    ConsumerError,
    check_unique_names,
    consumption_points,
    parse_change_path,
    parse_consumer_dict,
    parse_consumer_file,
    parsed_object_paths,
    path_affects,
)
from datacontract.parser import parse_dict


def _profile(spec, **kwargs):
    return parse_consumer_dict({"name": "c1", "contract": spec, **kwargs})


class ProfileParseTest(unittest.TestCase):
    def test_minimal_profile_defaults(self):
        p = _profile({"root": "string"})
        self.assertEqual(p.name, "c1")
        self.assertFalse(p.critical)
        self.assertFalse(p.strict)
        self.assertIsNotNone(p.contract)

    def test_full_profile(self):
        p = parse_consumer_dict({
            "name": "payment",
            "critical": True,
            "strict": True,
            "contract": {"version": "1", "root": "integer"},
        })
        self.assertTrue(p.critical)
        self.assertTrue(p.strict)
        self.assertEqual(p.contract.version, "1")

    def test_missing_name_rejected(self):
        with self.assertRaises(ConsumerError):
            parse_consumer_dict({"contract": {"root": "string"}})

    def test_missing_contract_rejected(self):
        with self.assertRaises(ConsumerError):
            parse_consumer_dict({"name": "c1"})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConsumerError):
            parse_consumer_dict(
                {"name": "c1", "contract": {"root": "string"}, "critcal": True}
            )

    def test_non_bool_flags_rejected(self):
        with self.assertRaises(ConsumerError):
            parse_consumer_dict(
                {"name": "c1", "contract": {"root": "string"}, "critical": "yes"}
            )

    def test_invalid_inner_contract_rejected(self):
        with self.assertRaises(ConsumerError) as ctx:
            parse_consumer_dict(
                {"name": "c1", "contract": {"root": {"type": "string", "enum": []}}}
            )
        self.assertIn("c1", str(ctx.exception))

    def test_contract_supports_defs_and_refs(self):
        p = _profile({
            "defs": {"Addr": {"type": "object", "fields": [
                {"name": "city", "type": "string"}]}},
            "root": {"type": "object", "fields": [
                {"name": "address", "type": "Addr"}]},
        })
        self.assertIn(("address", "city"), p.consumption_points())

    def test_parse_file_and_duplicate_names(self):
        with TemporaryDirectory() as d:
            p1 = Path(d) / "a.json"
            p1.write_text(json.dumps(
                {"name": "x", "contract": {"root": "string"}}), encoding="utf-8")
            profile = parse_consumer_file(p1)
            self.assertEqual(profile.name, "x")
            with self.assertRaises(ConsumerError):
                check_unique_names([profile, profile])

    def test_parse_file_invalid_json(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConsumerError):
                parse_consumer_file(p)


class ConsumptionPointsTest(unittest.TestCase):
    def test_flat_object(self):
        points = consumption_points(parse_dict(
            {"root": {"type": "object", "fields": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string", "optional": True},
            ]}}
        ))
        self.assertEqual(points, frozenset({("id",), ("name",)}))

    def test_nested_object_only_declared_fields(self):
        # 下游只声明它消费的子字段：address.zip 不在消费点内
        points = consumption_points(parse_dict(
            {"root": {"type": "object", "fields": [
                {"name": "address", "type": {"type": "object", "fields": [
                    {"name": "city", "type": "string"}]}},
            ]}}
        ))
        self.assertEqual(points, frozenset({("address", "city")}))

    def test_array_element_path_collapses_brackets(self):
        points = consumption_points(parse_dict(
            {"root": {"type": "object", "fields": [
                {"name": "items", "type": {"type": "array", "items": {
                    "type": "object", "fields": [
                        {"name": "id", "type": "integer"}]}}},
            ]}}
        ))
        self.assertEqual(points, frozenset({("items", "id")}))

    def test_scalar_root(self):
        self.assertEqual(consumption_points(parse_dict({"root": "string"})), frozenset({()}))

    def test_recursive_ref_truncates(self):
        points = consumption_points(parse_dict({
            "defs": {"Node": {"type": "object", "fields": [
                {"name": "id", "type": "integer"},
                {"name": "children", "type": {"type": "array", "items": "Node"}},
            ]}},
            "root": "Node",
        }))
        # 递归处截断：children 的 Ref 本身成为消费点叶子
        self.assertIn(("id",), points)
        self.assertIn(("children",), points)
        self.assertNotIn(("children", "id"), points)

    def test_parsed_object_paths_includes_root_and_ancestors(self):
        points = frozenset({("address", "city"), ("id",)})
        objects = parsed_object_paths(points)
        self.assertIn((), objects)
        self.assertIn(("address",), objects)
        self.assertNotIn(("id", "x"), objects)


class ChangePathTest(unittest.TestCase):
    def test_root(self):
        self.assertEqual(parse_change_path("$"), ())

    def test_nested(self):
        self.assertEqual(parse_change_path("$.a.b"), ("a", "b"))

    def test_array_brackets_stripped(self):
        self.assertEqual(parse_change_path("$.items[].id"), ("items", "id"))
        self.assertEqual(parse_change_path("$[].kind"), ("kind",))

    def test_defs_path_parses_loosely(self):
        # $defs 路径在投影前已被过滤，这里只要求不炸
        self.assertEqual(parse_change_path("$defs.Addr"), ("defs", "Addr"))


class AffectsTest(unittest.TestCase):
    def setUp(self):
        # 下游消费：id、address.city、items[].id
        self.points = frozenset({("id",), ("address", "city"), ("items", "id")})

    def test_exact_hit(self):
        self.assertTrue(path_affects(("id",), self.points))

    def test_change_on_ancestor_of_point(self):
        # address 整个对象类型被改 → 影响读 address.city 的下游
        self.assertTrue(path_affects(("address",), self.points))

    def test_change_below_point(self):
        # 下游透传 id 子树时，更深层的变化也算影响
        self.assertTrue(path_affects(("id", "sub"), self.points))

    def test_sibling_subtree_not_affected(self):
        # 上游删了 address.zip：下游只读 address.city，不受影响
        self.assertFalse(path_affects(("address", "zip"), self.points))

    def test_unrelated_root_field_not_affected(self):
        self.assertFalse(path_affects(("email",), self.points))

    def test_root_change_affects_everything(self):
        self.assertTrue(path_affects((), self.points))

    def test_array_element_change(self):
        self.assertTrue(path_affects(("items",), self.points))
        self.assertTrue(path_affects(("items", "id"), self.points))
        self.assertFalse(path_affects(("items", "name"), self.points))


if __name__ == "__main__":
    unittest.main()
