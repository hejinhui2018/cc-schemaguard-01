"""结构化版本差异计算。

不是文本 diff：沿两份契约的规范化 IR 同步递归，产出一组带
**数据路径**（``$.user.address.city``、``$.items[].id`` 风格）的
:class:`Change`。枚举增删、可选性、默认值、字段增删、类型变化
分别归类，供兼容性引擎逐条归因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .model import (
    ArrayType,
    Contract,
    Field,
    ObjectType,
    RefType,
    ScalarType,
    TypeNode,
    describe_type,
)
from .parser import _freeze  # 复用规范化（内部助手）


class ChangeKind(str, Enum):
    # 类型
    TYPE_CHANGED = "type_changed"
    # 枚举
    ENUM_EXTENDED = "enum_extended"   # 已有枚举上新增成员
    ENUM_REDUCED = "enum_reduced"     # 已有枚举上移除成员
    ENUM_REPLACED = "enum_replaced"   # 既增又减
    ENUM_CONSTRAINED = "enum_constrained"  # 原无约束 → 新增枚举约束（取值空间收窄）
    ENUM_UNCONSTRAINED = "enum_unconstrained"  # 移除枚举约束（取值空间放开）
    # 对象字段
    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    FIELD_MADE_REQUIRED = "field_made_required"
    FIELD_MADE_OPTIONAL = "field_made_optional"
    # 默认值
    DEFAULT_ADDED = "default_added"
    DEFAULT_REMOVED = "default_removed"
    DEFAULT_CHANGED = "default_changed"
    # 数组/对象元信息
    ADDITIONAL_PROPERTIES_CHANGED = "additional_properties_changed"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    path: str
    message: str
    old: Any = None
    new: Any = None
    field: str | None = None
    # 判定规则需要的结构化元数据（可选性、是否带默认值、类型大类等）
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "field": self.field,
            "message": self.message,
            "old": _jsonable(self.old),
            "new": _jsonable(self.new),
            "meta": self.meta,
        }


@dataclass
class _Ctx:
    old_contract: Contract
    new_contract: Contract
    changes: list[Change] = field(default_factory=list)
    # 从 root 遍历时触达过的 (old_ref, new_ref) 对：用于跳过“孤岛 def”的重复比对
    _reached: set[tuple[str | None, str | None]] = field(default_factory=set)


def diff_contracts(old: Contract, new: Contract) -> list[Change]:
    """计算两份契约的结构化差异（按路径与 kind 稳定排序）。"""
    ctx = _Ctx(old_contract=old, new_contract=new)
    _diff_types(old.root, new.root, "$", ctx)
    # def 中未被 root 引用到的“孤岛定义”也参与比对（契约资产的一部分）
    _diff_unreferenced_defs(ctx)
    return sorted(ctx.changes, key=lambda c: (c.path, c.kind.value, c.field or ""))


def _diff_unreferenced_defs(ctx: _Ctx) -> None:
    old_names = set(ctx.old_contract.defs)
    new_names = set(ctx.new_contract.defs)
    for name in sorted(old_names - new_names):
        ctx.changes.append(
            Change(
                kind=ChangeKind.FIELD_REMOVED,
                path=f"$defs.{name}",
                message=f"可复用定义 {name!r} 被删除",
            )
        )
    for name in sorted(new_names - old_names):
        ctx.changes.append(
            Change(
                kind=ChangeKind.FIELD_ADDED,
                path=f"$defs.{name}",
                message=f"新增可复用定义 {name!r}",
            )
        )
    for name in sorted(old_names & new_names):
        # root 遍历已沿数据路径展开过该 def 对，这里不再重复
        if (name, name) in ctx._reached:
            continue
        _diff_types(
            ctx.old_contract.defs[name],
            ctx.new_contract.defs[name],
            f"$defs.{name}",
            ctx,
        )


def _resolve(contract: Contract, node: TypeNode) -> TypeNode:
    return contract.resolve(node)


def _diff_types(
    old: TypeNode,
    new: TypeNode,
    path: str,
    ctx: _Ctx,
    *,
    chain: frozenset[tuple[str | None, str | None]] = frozenset(),
) -> None:
    # 环检测按“递归链”进行：同一条链上重复出现同一 ref 对才停止，
    # 因此同一 def 被多个字段引用时，每一条数据路径都会独立展开。
    old_ref = old.ref if isinstance(old, RefType) else None
    new_ref = new.ref if isinstance(new, RefType) else None
    pair = (old_ref, new_ref) if (old_ref is not None or new_ref is not None) else None
    if pair is not None:
        if pair in chain:
            return
        chain = chain | {pair}
        ctx._reached.add(pair)

    old_r = _resolve(ctx.old_contract, old)
    new_r = _resolve(ctx.new_contract, new)

    # resolve 遇到纯自指环会返回 Ref 本身；取出其目标继续结构比对
    if isinstance(old_r, RefType):
        old_r = ctx.old_contract.defs.get(old_r.ref, old_r)
    if isinstance(new_r, RefType):
        new_r = ctx.new_contract.defs.get(new_r.ref, new_r)

    if isinstance(old_r, ScalarType) and isinstance(new_r, ScalarType):
        _diff_scalar(old_r, new_r, path, ctx)
    elif isinstance(old_r, ObjectType) and isinstance(new_r, ObjectType):
        _diff_object(old_r, new_r, path, ctx, chain)
    elif isinstance(old_r, ArrayType) and isinstance(new_r, ArrayType):
        _diff_types(old_r.items, new_r.items, f"{path}[]", ctx, chain=chain)
        _diff_default(old_r, new_r, path, ctx)
    else:
        ctx.changes.append(
            Change(
                kind=ChangeKind.TYPE_CHANGED,
                path=path,
                message=(
                    f"类型形态变化: {describe_type(old_r)} → {describe_type(new_r)}"
                ),
                old=describe_type(old_r),
                new=describe_type(new_r),
                meta={"category": "form"},
            )
        )


def _diff_scalar(old: ScalarType, new: ScalarType, path: str, ctx: _Ctx) -> None:
    if old.name != new.name:
        ctx.changes.append(
            Change(
                kind=ChangeKind.TYPE_CHANGED,
                path=path,
                message=f"标量类型变化: {old.name} → {new.name}",
                old=old.name,
                new=new.name,
                meta={"category": "scalar"},
            )
        )

    old_enum = set(old.enum)
    new_enum = set(new.enum)
    if old_enum or new_enum:
        added = [v for v in new.enum if v not in old_enum]
        removed = [v for v in old.enum if v not in new_enum]
        if not old_enum and new_enum:
            # 原本“任意字符串”都合法，现在被收窄为固定集合
            kind = ChangeKind.ENUM_CONSTRAINED
            msg = f"新增枚举约束，取值被限制为: {list(new.enum)}"
        elif old_enum and not new_enum:
            kind = ChangeKind.ENUM_UNCONSTRAINED
            msg = f"移除枚举约束（原取值集合 {list(old.enum)}）"
        elif added and removed:
            kind = ChangeKind.ENUM_REPLACED
            msg = f"枚举取值被替换: 移除 {removed}，新增 {added}"
        elif added:
            kind = ChangeKind.ENUM_EXTENDED
            msg = f"枚举新增取值: {added}"
        elif removed:
            kind = ChangeKind.ENUM_REDUCED
            msg = f"枚举移除取值: {removed}"
        else:
            kind = None
        if kind is not None:
            ctx.changes.append(
                Change(kind=kind, path=path, message=msg, old=list(old.enum), new=list(new.enum))
            )

    _diff_default(old, new, path, ctx)


def _diff_object(old: ObjectType, new: ObjectType, path: str, ctx: _Ctx, chain: frozenset = frozenset()) -> None:
    old_fields = {f.name: f for f in old.fields}
    new_fields = {f.name: f for f in new.fields}

    for name in sorted(new_fields.keys() - old_fields.keys()):
        f = new_fields[name]
        fpath = f"{path}.{name}"
        status = "可选" if f.optional else "必填"
        default_note = "，带默认值" if _has_default(f.type) else ""
        ctx.changes.append(
            Change(
                kind=ChangeKind.FIELD_ADDED,
                path=fpath,
                field=name,
                message=f"新增{status}字段 {name!r}（{describe_type(f.type)}）{default_note}",
                old=None,
                new=describe_type(f.type),
                meta={"optional": f.optional, "has_default": _has_default(f.type)},
            )
        )

    for name in sorted(old_fields.keys() - new_fields.keys()):
        f = old_fields[name]
        fpath = f"{path}.{name}"
        status = "可选" if f.optional else "必填"
        ctx.changes.append(
            Change(
                kind=ChangeKind.FIELD_REMOVED,
                path=fpath,
                field=name,
                message=f"删除{status}字段 {name!r}（原类型 {describe_type(f.type)}）",
                old=describe_type(f.type),
                new=None,
                meta={"optional": f.optional, "has_default": _has_default(f.type)},
            )
        )

    for name in sorted(old_fields.keys() & new_fields.keys()):
        of, nf = old_fields[name], new_fields[name]
        fpath = f"{path}.{name}"
        if of.optional and not nf.optional:
            ctx.changes.append(
                Change(
                    kind=ChangeKind.FIELD_MADE_REQUIRED,
                    path=fpath,
                    field=name,
                    message=f"字段 {name!r} 由可选变为必填",
                    old="optional",
                    new="required",
                )
            )
        elif not of.optional and nf.optional:
            ctx.changes.append(
                Change(
                    kind=ChangeKind.FIELD_MADE_OPTIONAL,
                    path=fpath,
                    field=name,
                    message=f"字段 {name!r} 由必填变为可选",
                    old="required",
                    new="optional",
                )
            )
        _diff_types(of.type, nf.type, fpath, ctx, chain=chain)

    if old.additional_properties != new.additional_properties:
        ctx.changes.append(
            Change(
                kind=ChangeKind.ADDITIONAL_PROPERTIES_CHANGED,
                path=path,
                message=(
                    f"additionalProperties: {old.additional_properties} → {new.additional_properties}"
                ),
                old=old.additional_properties,
                new=new.additional_properties,
            )
        )

    _diff_default(old, new, path, ctx)


def _diff_default(old: TypeNode, new: TypeNode, path: str, ctx: _Ctx) -> None:
    old_has = getattr(old, "has_default", False)
    new_has = getattr(new, "has_default", False)
    old_val = _freeze(getattr(old, "default", None))
    new_val = _freeze(getattr(new, "default", None))

    if new_has and not old_has:
        ctx.changes.append(
            Change(
                kind=ChangeKind.DEFAULT_ADDED,
                path=path,
                message=f"新增默认值: {_preview(new_val)}",
                old=None,
                new=_jsonable(new_val),
            )
        )
    elif old_has and not new_has:
        ctx.changes.append(
            Change(
                kind=ChangeKind.DEFAULT_REMOVED,
                path=path,
                message=f"移除默认值（原默认值 {_preview(old_val)}）",
                old=_jsonable(old_val),
                new=None,
            )
        )
    elif old_has and new_has and old_val != new_val:
        ctx.changes.append(
            Change(
                kind=ChangeKind.DEFAULT_CHANGED,
                path=path,
                message=f"默认值变化: {_preview(old_val)} → {_preview(new_val)}",
                old=_jsonable(old_val),
                new=_jsonable(new_val),
            )
        )


def _has_default(node: TypeNode) -> bool:
    return getattr(node, "has_default", False)


def _preview(value: Any, limit: int = 40) -> str:
    import json

    try:
        text = json.dumps(_jsonable(value), ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _jsonable(value: Any) -> Any:
    from types import MappingProxyType

    if isinstance(value, MappingProxyType):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value
