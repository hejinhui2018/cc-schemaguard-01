"""契约解析与规范化测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from datacontract.model import ArrayType, ObjectType, RefType, ScalarType
from datacontract.parser import ContractError, parse_dict, parse_file, parse_text

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"


class ParseScalarTest(unittest.TestCase):
    def test_bare_scalar_names(self):
        for name in ("string", "integer", "number", "boolean", "null"):
            contract = parse_dict({"root": name})
            self.assertIsInstance(contract.root, ScalarType)
            self.assertEqual(contract.root.name, name)

    def test_enum_and_default(self):
        contract = parse_dict(
            {"root": {"type": "string", "enum": ["A", "B"], "default": "A"}}
        )
        node = contract.root
        self.assertEqual(node.enum, ("A", "B"))
        self.assertTrue(node.has_default)
        self.assertEqual(node.default, "A")

    def test_explicit_null_default_counts(self):
        contract = parse_dict({"root": {"type": "null", "default": None}})
        self.assertTrue(contract.root.has_default)

    def test_enum_requires_nonempty(self):
        with self.assertRaisesRegex(ContractError, r"enum"):
            parse_dict({"root": {"type": "string", "enum": []}})

    def test_enum_duplicate_rejected(self):
        with self.assertRaisesRegex(ContractError, r"枚举值重复"):
            parse_dict({"root": {"type": "string", "enum": ["A", "A"]}})

    def test_enum_typed(self):
        with self.assertRaisesRegex(ContractError, r"不是 integer"):
            parse_dict({"root": {"type": "integer", "enum": [1, "x"]}})

    def test_bool_is_not_integer(self):
        with self.assertRaises(ContractError):
            parse_dict({"root": {"type": "integer", "enum": [True]}})

    def test_default_outside_enum_rejected(self):
        with self.assertRaisesRegex(ContractError, r"不在枚举集合内"):
            parse_dict(
                {"root": {"type": "string", "enum": ["A", "B"], "default": "C"}}
            )

    def test_unknown_scalar(self):
        with self.assertRaisesRegex(ContractError, r"未知类型"):
            parse_dict({"root": "datetime"})


class ParseObjectTest(unittest.TestCase):
    def test_fields_order_preserved(self):
        contract = parse_dict(
            {
                "root": {
                    "type": "object",
                    "fields": [
                        {"name": "z", "type": "string"},
                        {"name": "a", "type": "integer"},
                    ],
                }
            }
        )
        self.assertEqual([f.name for f in contract.root.fields], ["z", "a"])

    def test_optional_and_field_default(self):
        contract = parse_dict(
            {
                "root": {
                    "type": "object",
                    "fields": [
                        {"name": "a", "type": "integer", "optional": True, "default": 0}
                    ],
                }
            }
        )
        f = contract.root.fields[0]
        self.assertTrue(f.optional)
        self.assertTrue(f.type.has_default)
        self.assertEqual(f.type.default, 0)

    def test_duplicate_field_rejected(self):
        with self.assertRaisesRegex(ContractError, r"重复"):
            parse_dict(
                {
                    "root": {
                        "type": "object",
                        "fields": [
                            {"name": "a", "type": "string"},
                            {"name": "a", "type": "integer"},
                        ],
                    }
                }
            )

    def test_missing_field_type(self):
        with self.assertRaisesRegex(ContractError, r"缺少 type"):
            parse_dict({"root": {"type": "object", "fields": [{"name": "a"}]}})

    def test_unknown_field_key(self):
        with self.assertRaisesRegex(ContractError, r"不支持的键"):
            parse_dict(
                {"root": {"type": "object", "fields": [
                    {"name": "a", "type": "string", "format": "email"}
                ]}}
            )

    def test_nested_object_default_missing_required(self):
        with self.assertRaisesRegex(ContractError, r"缺少必填字段"):
            parse_dict(
                {
                    "root": {
                        "type": "object",
                        "fields": [
                            {
                                "name": "addr",
                                "type": {
                                    "type": "object",
                                    "fields": [{"name": "city", "type": "string"}],
                                },
                                "default": {},
                            }
                        ],
                    }
                }
            )

    def test_nested_object_default_unknown_field(self):
        with self.assertRaisesRegex(ContractError, r"未声明字段"):
            parse_dict(
                {
                    "root": {
                        "type": "object",
                        "fields": [
                            {
                                "name": "addr",
                                "type": {
                                    "type": "object",
                                    "fields": [{"name": "city", "type": "string"}],
                                },
                                "default": {"city": "X", "zip": "0"},
                            }
                        ],
                    }
                }
            )


class ParseArrayTest(unittest.TestCase):
    def test_array_requires_items(self):
        with self.assertRaisesRegex(ContractError, r"缺少 items"):
            parse_dict({"root": {"type": "array"}})

    def test_nested_array(self):
        contract = parse_dict({"root": {"type": "array", "items": {"type": "array", "items": "integer"}}})
        self.assertIsInstance(contract.root, ArrayType)
        self.assertIsInstance(contract.root.items, ArrayType)
        self.assertEqual(contract.root.items.items.name, "integer")

    def test_array_default_typed(self):
        contract = parse_dict(
            {"root": {"type": "array", "items": "integer", "default": [1, 2]}}
        )
        self.assertEqual(contract.root.default, (1, 2))

    def test_array_default_wrong_element_type(self):
        with self.assertRaisesRegex(ContractError, r"不是 integer"):
            parse_dict(
                {"root": {"type": "array", "items": "integer", "default": [1, "x"]}}
            )


class ParseRefTest(unittest.TestCase):
    def test_ref_in_defs(self):
        contract = parse_dict(
            {
                "defs": {"Address": {"type": "object", "fields": [
                    {"name": "city", "type": "string"}
                ]}},
                "root": {"$ref": "Address"},
            }
        )
        self.assertIsInstance(contract.root, RefType)
        resolved = contract.resolve(contract.root)
        self.assertIsInstance(resolved, ObjectType)

    def test_forward_reference(self):
        contract = parse_dict(
            {
                "defs": {
                    "A": {"type": "object", "fields": [{"name": "b", "type": "B"}]},
                    "B": {"type": "string"},
                },
                "root": "A",
            }
        )
        a = contract.resolve(contract.root)
        self.assertEqual(a.fields[0].type.ref, "B")

    def test_recursive_definition(self):
        contract = parse_dict(
            {
                "defs": {
                    "Node": {
                        "type": "object",
                        "fields": [
                            {"name": "id", "type": "integer"},
                            {"name": "children", "type": {"type": "array", "items": "Node"}},
                        ],
                    }
                },
                "root": "Node",
            }
        )
        node = contract.resolve(contract.root)
        self.assertIsInstance(node, ObjectType)
        children = node.fields[1].type.items
        self.assertEqual(children.ref, "Node")

    def test_dangling_ref(self):
        with self.assertRaisesRegex(ContractError, r"未定义"):
            parse_dict({"root": {"$ref": "Nope"}})

    def test_ref_with_extra_keys(self):
        with self.assertRaisesRegex(ContractError, r"不能同时携带"):
            parse_dict(
                {
                    "defs": {"A": {"type": "string"}},
                    "root": {"$ref": "A", "optional": True},
                }
            )

    def test_default_directly_on_ref_rejected(self):
        with self.assertRaisesRegex(ContractError, r"不能对 \$ref"):
            parse_dict(
                {
                    "defs": {"A": {"type": "string"}},
                    "root": {
                        "type": "object",
                        "fields": [{"name": "a", "type": {"$ref": "A"}, "default": "x"}],
                    },
                }
            )


class ParseEnvelopeTest(unittest.TestCase):
    def test_root_form(self):
        contract = parse_dict({"version": "9", "root": "string"})
        self.assertEqual(contract.version, "9")
        self.assertEqual(contract.root.name, "string")

    def test_embedded_root_form(self):
        contract = parse_dict(
            {"version": "9", "type": "object", "fields": [
                {"name": "a", "type": "string"}
            ]}
        )
        self.assertIsInstance(contract.root, ObjectType)

    def test_missing_root(self):
        with self.assertRaisesRegex(ContractError, r"缺少 root"):
            parse_dict({"version": "9"})

    def test_bad_json(self):
        with self.assertRaisesRegex(ContractError, r"不是合法 JSON"):
            parse_text("{not json")

    def test_version_must_be_string(self):
        with self.assertRaisesRegex(ContractError, r"version"):
            parse_dict({"version": 3, "root": "string"})

    def test_file_roundtrip(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"root": "boolean"}), encoding="utf-8")
            contract = parse_file(p)
            self.assertEqual(contract.root.name, "boolean")

    def test_example_contracts_parse(self):
        for name in ("user_v1.json", "user_v2_compatible.json", "user_v2_breaking.json"):
            with self.subTest(name=name):
                contract = parse_file(EXAMPLES / name)
                self.assertIsNotNone(contract.version)


if __name__ == "__main__":
    unittest.main()
