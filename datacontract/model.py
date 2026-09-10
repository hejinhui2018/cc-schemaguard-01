"""规范化内部表示（IR）。

解析后的契约是一棵不可变的类型树：

- ScalarType：string / integer / number / boolean / null
- ObjectType：具名字段集合（字段顺序保留，便于稳定输出）
- ArrayType：元素类型
- RefType：指向 ``$defs`` 中某个具名类型的引用（支持递归结构）

所有上层能力（diff / 兼容性 / 迁移规划）都只依赖这里的结构，
不再接触原始 JSON dict。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Mapping, Union

SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})

# 标量之间的“加宽”关系也显式建模在兼容性引擎里，这里只做存在性声明。


@dataclass(frozen=True)
class TypeNode:
    """所有类型节点的基类。"""

    def accept(self, visitor: "TypeVisitor") -> Any:  # pragma: no cover - 抽象方法
        raise NotImplementedError


@dataclass(frozen=True)
class ScalarType(TypeNode):
    """基础标量类型。"""

    name: str
    enum: tuple[Any, ...] = ()
    # 默认值经过规范化（枚举/类型校验在 parser 层完成）
    has_default: bool = False
    default: Any = None

    def accept(self, visitor: "TypeVisitor") -> Any:
        return visitor.visit_scalar(self)


@dataclass(frozen=True)
class Field:
    """对象字段：名称 + 类型 + 可选性 + 描述。

    默认值挂在类型节点上（标量、枚举、数组、对象都可能有默认值）。
    """

    name: str
    type: TypeNode
    optional: bool = False
    description: str | None = None


@dataclass(frozen=True)
class ObjectType(TypeNode):
    """对象类型；fields 按声明顺序保存。"""

    fields: tuple[Field, ...] = ()
    additional_properties: bool = False
    has_default: bool = False
    default: Mapping[str, Any] | None = None

    def field(self, name: str) -> Field | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def accept(self, visitor: "TypeVisitor") -> Any:
        return visitor.visit_object(self)


@dataclass(frozen=True)
class ArrayType(TypeNode):
    """数组类型。"""

    items: TypeNode
    has_default: bool = False
    default: tuple[Any, ...] | None = None

    def accept(self, visitor: "TypeVisitor") -> Any:
        return visitor.visit_array(self)


@dataclass(frozen=True)
class RefType(TypeNode):
    """对 ``$defs`` 中具名类型的引用。

    解析期只记录名字；:meth:`Contract.resolve` 负责解引用，
    这样递归结构（如树节点）也能表达。
    """

    ref: str

    def accept(self, visitor: "TypeVisitor") -> Any:
        return visitor.visit_ref(self)


@dataclass(frozen=True)
class Contract:
    """一份规范化契约。"""

    version: str | None
    root: TypeNode
    defs: Mapping[str, TypeNode] = dc_field(default_factory=dict)

    def resolve(self, node: TypeNode) -> TypeNode:
        """沿单条 Ref 链解引用一次到底（非 Ref 原样返回）。"""
        seen: set[str] = set()
        while isinstance(node, RefType):
            if node.ref in seen:
                # 递归定义的“底”就是 Ref 本身；上层遍历时用 _DerefWalker 防环。
                return node
            seen.add(node.ref)
            target = self.defs.get(node.ref)
            if target is None:  # pragma: no cover - parser 已保证
                raise KeyError(f"未定义的引用: {node.ref}")
            node = target
        return node


class TypeVisitor:  # pragma: no cover - 结构示意
    def visit_scalar(self, node: ScalarType) -> Any:
        raise NotImplementedError

    def visit_object(self, node: ObjectType) -> Any:
        raise NotImplementedError

    def visit_array(self, node: ArrayType) -> Any:
        raise NotImplementedError

    def visit_ref(self, node: RefType) -> Any:
        raise NotImplementedError


def describe_type(node: TypeNode, defs: Mapping[str, TypeNode] | None = None) -> str:
    """生成紧凑的人类可读类型描述，用于报告与错误信息。"""
    if isinstance(node, RefType):
        return f"${node.ref}"
    if isinstance(node, ScalarType):
        base = node.name
        if node.enum:
            members = "|".join(jsonish_literal(v) for v in node.enum)
            base = f"{base}({members})"
        return base
    if isinstance(node, ArrayType):
        return f"array<{describe_type(node.items, defs)}>"
    if isinstance(node, ObjectType):
        parts = []
        for f in node.fields:
            mark = "?" if f.optional else ""
            parts.append(f"{f.name}{mark}: {describe_type(f.type, defs)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(f"未知类型节点: {node!r}")  # pragma: no cover


def jsonish_literal(value: Any) -> str:
    """报告用的字面量渲染（非正式 JSON 序列化）。"""
    if isinstance(value, str):
        return value if len(value) <= 24 else value[:21] + "..."
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


# 便于类型注解使用
AnyType = Union[ScalarType, ObjectType, ArrayType, RefType]
