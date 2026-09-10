"""契约 JSON 解析与规范化。

支持的契约写法见 README。要点：

- 顶层形如 ``{"version": "1.0", "defs": {...}, "root": <类型声明>}``；
  若没有 ``root``，且顶层本身带 ``type``，则顶层即根类型声明。
- 类型声明：

  - 标量：``{"type": "string", "enum": ["A", "B"], "default": "A"}``
  - 对象：``{"type": "object", "fields": [{"name": ..., "type": ...,
    "optional": false, "default": ..., "description": ...}]}``
  - 数组：``{"type": "array", "items": <类型声明>}``
  - 引用：``{"$ref": "Address"}``，def 内可前向引用、可递归引用
- 字段的 ``type`` 可以直接写标量名（``"string"``）或嵌套声明。

解析分两阶段：先构造完整 IR（引用只记名），再统一做默认值/枚举等
语义校验，因此允许前向引用与递归定义。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .model import (
    SCALAR_TYPES,
    ArrayType,
    Contract,
    Field,
    ObjectType,
    RefType,
    ScalarType,
    TypeNode,
)

# def 两阶段解析的占位符：先登记名字，随后立即填充真实 IR
_PENDING: TypeNode = ScalarType(name="null")

class ContractError(ValueError):
    """契约本身非法（语法/语义错误）。``path`` 指向问题位置。"""

    def __init__(self, message: str, path: str = "$"):
        self.path = path
        super().__init__(f"{path}: {message}")


def parse_text(text: str, *, source: str = "<string>") -> Contract:
    """从 JSON 文本解析。"""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"不是合法 JSON（{source}:{exc.lineno}:{exc.colno}）: {exc.msg}") from exc
    return parse_dict(raw)


def parse_file(path: str | Path) -> Contract:
    """从 .json 文件解析。"""
    p = Path(path)
    return parse_text(p.read_text(encoding="utf-8"), source=str(p))


def parse_contract(raw: str | Mapping[str, Any] | Path) -> Contract:
    """便利入口：接受 dict、JSON 字符串或路径。"""
    if isinstance(raw, Mapping):
        return parse_dict(raw)
    if isinstance(raw, (str, Path)):
        text = Path(raw).read_text(encoding="utf-8") if isinstance(raw, Path) else raw
        source = str(raw) if isinstance(raw, Path) else "<string>"
        return parse_text(text, source=source)
    raise TypeError(f"无法解析 {type(raw).__name__} 类型的契约输入")


def parse_dict(raw: Mapping[str, Any]) -> Contract:
    """从已反序列化的 dict 解析并规范化。"""
    if not isinstance(raw, Mapping):
        raise ContractError("契约顶层必须是对象")

    version = raw.get("version")
    if version is not None and not isinstance(version, str):
        raise ContractError("version 必须是字符串", "$.version")

    defs: dict[str, TypeNode] = {}
    raw_defs = raw.get("defs", {})
    if raw_defs is None:
        raw_defs = {}
    if not isinstance(raw_defs, Mapping):
        raise ContractError("defs 必须是对象", "$.defs")

    parser = _Parser(defs)

    # 第一阶段 a：先登记所有 def 名字（占位），使函数体内可前向引用/递归引用
    for name in raw_defs:
        if not isinstance(name, str) or not name:
            raise ContractError("def 名称必须是非空字符串", f"$.defs.{name}")
        if name in defs:
            raise ContractError(f"def {name!r} 重复定义", f"$.defs.{name}")
        defs[name] = _PENDING  # type: ignore[assignment]

    # 第一阶段 b：解析所有 def 函数体（此时任何 def 名字都“已声明”）
    for name, spec in raw_defs.items():
        defs[name] = parser.parse_type(spec, f"$.defs.{name}")

    # 根节点
    if "root" in raw:
        root = parser.parse_type(raw["root"], "$.root")
    elif "type" in raw or "$ref" in raw:
        # 顶层本身即根类型声明：剥掉信封键（version/defs）后再解析
        root_spec = {k: v for k, v in raw.items() if k not in ("version", "defs")}
        root = parser.parse_type(root_spec, "$")
    else:
        raise ContractError("契约缺少 root（顶层也不是一个类型声明）")

    contract = Contract(version=version, root=root, defs=dict(defs))

    # 第二阶段：跨节点语义校验（默认值、枚举、引用目标）
    validator = _Validator(contract)
    validator.validate_all()
    return contract


class _Parser:
    def __init__(self, defs: dict[str, TypeNode]):
        self._defs = defs

    def parse_type(self, spec: Any, path: str) -> TypeNode:
        if isinstance(spec, str):
            # 裸字符串：标量名或对 def 的引用
            if spec in SCALAR_TYPES:
                return ScalarType(name=spec)
            if spec in self._defs:
                return RefType(ref=spec)
            raise ContractError(
                f"未知类型 {spec!r}（既不是标量也不是已声明的 def）", path
            )

        if not isinstance(spec, Mapping):
            raise ContractError("类型声明必须是字符串或对象", path)

        if "$ref" in spec:
            ref = spec["$ref"]
            if not isinstance(ref, str) or not ref:
                raise ContractError("$ref 必须是非空字符串", f"{path}.$ref")
            if len(spec) > 1:
                raise ContractError("$ref 声明不能同时携带其他键", path)
            if ref not in self._defs:
                raise ContractError(f"引用了未定义的 def {ref!r}", f"{path}.$ref")
            return RefType(ref=ref)

        kind = spec.get("type")
        if not isinstance(kind, str):
            raise ContractError("缺少 type（或 $ref）", path)

        if kind in SCALAR_TYPES:
            return self._parse_scalar(kind, spec, path)
        if kind == "object":
            return self._parse_object(spec, path)
        if kind == "array":
            return self._parse_array(spec, path)
        raise ContractError(f"未知类型 {kind!r}", f"{path}.type")

    def _parse_scalar(self, kind: str, spec: Mapping[str, Any], path: str) -> ScalarType:
        extra = set(spec) - {"type", "enum", "default", "description"}
        if extra:
            raise ContractError(f"标量上存在不支持的键: {sorted(extra)}", path)

        enum_values: tuple[Any, ...] = ()
        if "enum" in spec and spec["enum"] is not None:
            raw_enum = spec["enum"]
            if not isinstance(raw_enum, list) or not raw_enum:
                raise ContractError("enum 必须是非空数组", f"{path}.enum")
            values: list[Any] = []
            for i, v in enumerate(raw_enum):
                self._check_scalar_value(kind, v, f"{path}.enum[{i}]")
                if v in values:
                    raise ContractError(f"枚举值重复: {v!r}", f"{path}.enum[{i}]")
                values.append(v)
            enum_values = tuple(values)

        # default 显式为 null 也算“有默认值”（null 本身可能就是期望值）
        has_default = "default" in spec
        default = spec.get("default")

        node = ScalarType(name=kind, enum=enum_values, has_default=has_default, default=default)
        # 实际值校验延后到第二阶段（统一错误路径风格），这里先返回
        return node

    def _parse_object(self, spec: Mapping[str, Any], path: str) -> ObjectType:
        extra = set(spec) - {"type", "fields", "additionalProperties", "default", "description"}
        if extra:
            raise ContractError(f"对象上存在不支持的键: {sorted(extra)}", path)

        raw_fields = spec.get("fields", [])
        if not isinstance(raw_fields, list):
            raise ContractError("fields 必须是数组", f"{path}.fields")

        fields: list[Field] = []
        seen: set[str] = set()
        for i, raw_field in enumerate(raw_fields):
            fpath = f"{path}.fields[{i}]"
            if not isinstance(raw_field, Mapping):
                raise ContractError("字段声明必须是对象", fpath)
            name = raw_field.get("name")
            if not isinstance(name, str) or not name:
                raise ContractError("字段 name 必须是非空字符串", f"{fpath}.name")
            if name in seen:
                raise ContractError(f"字段 {name!r} 重复", f"{fpath}.name")
            seen.add(name)

            if "type" not in raw_field:
                raise ContractError(f"字段 {name!r} 缺少 type", fpath)
            ftype = self.parse_type(raw_field["type"], f"{fpath}.type")

            optional = raw_field.get("optional", False)
            if not isinstance(optional, bool):
                raise ContractError("optional 必须是布尔值", f"{fpath}.optional")

            description = raw_field.get("description")
            if description is not None and not isinstance(description, str):
                raise ContractError("description 必须是字符串", f"{fpath}.description")

            unexpected = set(raw_field) - {"name", "type", "optional", "default", "description"}
            if unexpected:
                raise ContractError(f"字段上存在不支持的键: {sorted(unexpected)}", fpath)

            has_field_default = "default" in raw_field
            field_default = raw_field.get("default")
            if has_field_default:
                # 把字段级默认值挂到类型节点上（包一层不可变外壳）
                ftype = self._attach_default(ftype, field_default)

            fields.append(Field(name=name, type=ftype, optional=optional, description=description))

        additional = spec.get("additionalProperties", False)
        if not isinstance(additional, bool):
            raise ContractError("additionalProperties 必须是布尔值", f"{path}.additionalProperties")

        has_default = "default" in spec
        default = _freeze(spec.get("default")) if has_default else None
        return ObjectType(
            fields=tuple(fields),
            additional_properties=additional,
            has_default=has_default,
            default=default,
        )

    def _parse_array(self, spec: Mapping[str, Any], path: str) -> ArrayType:
        extra = set(spec) - {"type", "items", "default", "description"}
        if extra:
            raise ContractError(f"数组上存在不支持的键: {sorted(extra)}", path)
        if "items" not in spec:
            raise ContractError("数组缺少 items", path)
        items = self.parse_type(spec["items"], f"{path}.items")
        has_default = "default" in spec
        default = _freeze(spec.get("default")) if has_default else None
        return ArrayType(items=items, has_default=has_default, default=default)

    def _attach_default(self, node: TypeNode, value: Any) -> TypeNode:
        """字段级 default 挂到对应类型节点（保持 frozen dataclass 风格）。"""
        frozen_value = _freeze(value)
        if isinstance(node, ScalarType):
            return ScalarType(
                name=node.name, enum=node.enum, has_default=True, default=value
            )
        if isinstance(node, ObjectType):
            return ObjectType(
                fields=node.fields,
                additional_properties=node.additional_properties,
                has_default=True,
                default=frozen_value,
            )
        if isinstance(node, ArrayType):
            return ArrayType(items=node.items, has_default=True, default=frozen_value)
        # default 直接挂在 Ref 上没有落点：要求写到 def 里
        raise ContractError("不能对 $ref 直接设置 default，请把 default 放到被引用的 def 上")

    @staticmethod
    def _check_scalar_value(kind: str, value: Any, path: str) -> None:
        if kind == "string":
            if not isinstance(value, str):
                raise ContractError(f"值 {value!r} 不是 string", path)
        elif kind == "integer":
            # 注意：bool 不是 int
            if not isinstance(value, int) or isinstance(value, bool):
                raise ContractError(f"值 {value!r} 不是 integer", path)
        elif kind == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ContractError(f"值 {value!r} 不是 number", path)
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ContractError(f"值 {value!r} 不是 boolean", path)
        elif kind == "null":
            if value is not None:
                raise ContractError(f"值 {value!r} 不是 null", path)


def _freeze(value: Any) -> Any:
    """把 JSON 反序列化结果转成不可变形态（list→tuple, dict→MappingProxy）。"""
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        from types import MappingProxyType

        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    return value


class _Validator:
    """第二阶段：默认值/枚举成员/引用可达性的递归校验。"""

    def __init__(self, contract: Contract):
        self.contract = contract

    def validate_all(self) -> None:
        for name, node in self.contract.defs.items():
            self._validate(node, f"$.defs.{name}", set())
        self._validate(self.contract.root, "$.root", set())

    def _validate(self, node: TypeNode, path: str, seen: set[str]) -> None:
        # 必须在解引用之前判 RefType，否则递归结构（Node.children -> Node）
        # 会绕过环检测无限下钻。
        if isinstance(node, RefType):
            if node.ref in seen:
                return
            target = self.contract.defs.get(node.ref)
            if target is None:  # pragma: no cover - 解析期已拦截悬空引用
                raise ContractError(f"引用了未定义的 def {node.ref!r}", path)
            self._validate(target, path, seen | {node.ref})
            return

        if isinstance(node, ScalarType):
            if node.has_default:
                _Parser._check_scalar_value(node.name, node.default, f"{path}.default")
                if node.enum and node.default not in node.enum:
                    raise ContractError(
                        f"默认值 {node.default!r} 不在枚举集合内", f"{path}.default"
                    )
            if node.name == "null" and node.enum:
                raise ContractError("null 类型不允许 enum", path)
        elif isinstance(node, ObjectType):
            if node.has_default:
                self._check_data(node, node.default, f"{path}.default", set())
            for f in node.fields:
                self._validate(f.type, f"{path}.fields.{f.name}", seen)
        elif isinstance(node, ArrayType):
            if node.has_default:
                if not isinstance(node.default, tuple):
                    raise ContractError("默认值必须是数组", f"{path}.default")
                for i, item in enumerate(node.default):
                    self._check_data(node.items, item, f"{path}.default[{i}]", set())
            self._validate(node.items, f"{path}.items", seen)

    def _check_data(self, node: TypeNode, value: Any, path: str, seen: set[str]) -> None:
        if isinstance(node, RefType):
            if node.ref in seen:
                return
            target = self.contract.defs.get(node.ref)
            if target is None:  # pragma: no cover
                raise ContractError(f"引用了未定义的 def {node.ref!r}", path)
            self._check_data(target, value, path, seen | {node.ref})
            return
        if isinstance(node, ScalarType):
            _Parser._check_scalar_value(node.name, value, path)
            if node.enum and value not in node.enum:
                raise ContractError(f"值 {value!r} 不在枚举集合内", path)
        elif isinstance(node, ArrayType):
            if not isinstance(value, (list, tuple)):
                raise ContractError(f"值 {value!r} 不是数组", path)
            for i, item in enumerate(value):
                self._check_data(node.items, item, f"{path}[{i}]", seen)
        elif isinstance(node, ObjectType):
            if not isinstance(value, Mapping):
                raise ContractError(f"值 {value!r} 不是对象", path)
            allowed = {f.name for f in node.fields}
            if not node.additional_properties:
                for key in value:
                    if key not in allowed:
                        raise ContractError(f"默认值出现未声明字段 {key!r}", f"{path}.{key}")
            for f in node.fields:
                if f.name in value:
                    self._check_data(f.type, value[f.name], f"{path}.{f.name}", seen)
                elif not f.optional and not _type_has_default(f.type, self.contract):
                    raise ContractError(f"默认值缺少必填字段 {f.name!r}", path)


def _type_has_default(node: TypeNode, contract: Contract) -> bool:
    node = contract.resolve(node)
    return getattr(node, "has_default", False)
