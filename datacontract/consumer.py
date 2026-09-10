"""下游消费画像（consumer profile）。

每个下游用一份画像声明「我实际消费的数据结构」：

.. code-block:: json

    {
      "name": "payment-service",
      "critical": true,
      "strict": false,
      "contract": {
        "root": {
          "type": "object",
          "fields": [
            {"name": "id", "type": "integer"},
            {"name": "status", "type": {"type": "string", "enum": ["ACTIVE", "DISABLED"]}}
          ]
        }
      }
    }

- ``name``：下游标识；评估结论与迁移步骤都按它归因；
- ``critical``：关键下游标记，治理放行口径（:mod:`datacontract.governance`）使用；
- ``strict``：``true`` 表示该下游严格解析（遇到未知字段即失败），
  上游新增字段对它将从「提示」升级为「阻断」；
- ``contract``：下游实际读取的字段与期望类型，写法与上游契约完全相同
  （支持 ``defs`` / ``$ref`` / 嵌套声明，直接复用 :mod:`datacontract.parser`）。
  字段范围只需要覆盖它**真正消费**的部分——上游多出来的字段不影响它，
  它用到的字段被改掉则一定会被评估发现。

画像在评估期被展开成一组**消费点**（consumption points）：从根出发、
沿字段/数组元素下钻到标量叶子的路径集合。上游的一条变更只要与任一
消费点「相等或互为前缀」，就判定为触及该下游。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import ArrayType, Contract, ObjectType, RefType, ScalarType, TypeNode
from .parser import ContractError, parse_dict


class ConsumerError(ValueError):
    """下游画像非法（缺字段 / 类型错误 / 消费契约本身非法）。"""


@dataclass(frozen=True)
class ConsumerProfile:
    """一个下游的消费画像。"""

    name: str
    contract: Contract
    critical: bool = False
    strict: bool = False

    def consumption_points(self) -> frozenset[tuple[str, ...]]:
        """该下游实际读取的叶子路径集合（段元组，根为 ``()``）。"""
        return consumption_points(self.contract)


# ---------------------------------------------------------------------------
# 画像解析
# ---------------------------------------------------------------------------

_KNOWN_KEYS = frozenset({"name", "critical", "strict", "contract"})


def parse_consumer_dict(raw: Mapping[str, Any], *, source: str = "<dict>") -> ConsumerProfile:
    """从 dict 解析下游画像并校验。"""
    if not isinstance(raw, Mapping):
        raise ConsumerError(f"下游画像必须是对象（{source}）")

    extra = set(raw) - _KNOWN_KEYS
    if extra:
        raise ConsumerError(f"下游画像存在不支持的键: {sorted(extra)}（{source}）")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConsumerError(f"下游画像缺少非空字符串 name（{source}）")

    critical = raw.get("critical", False)
    if not isinstance(critical, bool):
        raise ConsumerError(f"下游 {name!r} 的 critical 必须是布尔值（{source}）")

    strict = raw.get("strict", False)
    if not isinstance(strict, bool):
        raise ConsumerError(f"下游 {name!r} 的 strict 必须是布尔值（{source}）")

    if "contract" not in raw:
        raise ConsumerError(f"下游 {name!r} 缺少 contract（它实际消费的数据结构）（{source}）")
    try:
        contract = parse_dict(raw["contract"])
    except ContractError as exc:
        raise ConsumerError(f"下游 {name!r} 的消费契约非法: {exc}（{source}）") from exc

    return ConsumerProfile(name=name, contract=contract, critical=critical, strict=strict)


def parse_consumer_file(path: str | Path) -> ConsumerProfile:
    """从 .json 文件解析下游画像。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConsumerError(f"下游画像不是合法 JSON（{p}:{exc.lineno}:{exc.colno}）: {exc.msg}") from exc
    return parse_consumer_dict(raw, source=str(p))


def check_unique_names(profiles: Sequence[ConsumerProfile]) -> None:
    """同名下游直接报错（报告与迁移步骤将无法归因）。"""
    seen: set[str] = set()
    for profile in profiles:
        if profile.name in seen:
            raise ConsumerError(f"下游名称 {profile.name!r} 重复，评估结果将无法归因")
        seen.add(profile.name)


# ---------------------------------------------------------------------------
# 消费点提取与变更投影
# ---------------------------------------------------------------------------


def consumption_points(contract: Contract) -> frozenset[tuple[str, ...]]:
    """把消费契约展开为消费点集合。

    消费点 = 下游实际读取的叶子（标量、空对象、递归截断处）路径。
    段元组与 :func:`parse_change_path` 的规范化一致：根为 ``()``，
    数组元素不新增段（``$.items[].id`` → ``("items", "id")``）。
    """
    points: set[tuple[str, ...]] = set()

    def walk(node: TypeNode, segs: tuple[str, ...], chain: frozenset[str]) -> None:
        if isinstance(node, RefType):
            target = contract.defs.get(node.ref)
            # 递归引用截断：该子树结构与上层相同，Ref 本身成为消费点叶子
            if target is None or node.ref in chain:  # pragma: no cover - parser 已保证有定义
                points.add(segs)
                return
            walk(target, segs, chain | {node.ref})
            return
        if isinstance(node, ScalarType):
            points.add(segs)
            return
        if isinstance(node, ArrayType):
            walk(node.items, segs, chain)
            return
        if isinstance(node, ObjectType):
            if not node.fields:
                points.add(segs)
                return
            for f in node.fields:
                walk(f.type, segs + (f.name,), chain)
            return
        raise TypeError(f"未知类型节点: {node!r}")  # pragma: no cover

    walk(contract.root, (), frozenset())
    return frozenset(points)


def parse_change_path(path: str) -> tuple[str, ...]:
    """把 diff 产出的数据路径规范化为段元组。

    ``$`` → ``()``；``$.a.b`` → ``("a", "b")``；
    ``$.items[].id`` / ``$[].kind`` 的 ``[]`` 标记被剥掉（与消费点一致）。
    """
    if not path or path == "$":
        return ()
    body = path[1:] if path.startswith("$") else path
    segs: list[str] = []
    for part in body.split("."):
        while part.endswith("[]"):
            part = part[:-2]
        if part:
            segs.append(part)
    return tuple(segs)


def _is_prefix(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def path_affects(change_segs: tuple[str, ...], points: frozenset[tuple[str, ...]]) -> bool:
    """变更路径是否与任一消费点「相等或互为前缀」。

    - 变更打在消费点本身或其祖先（如整个对象类型被改）→ 影响；
    - 变更打在消费点之下（如下游透传的对象内部新增子字段）→ 影响；
    - 变更与消费点分属不同子树（上游多出/删除下游不读的字段）→ 不影响。
    """
    return any(
        _is_prefix(change_segs, point) or _is_prefix(point, change_segs)
        for point in points
    )


def parsed_object_paths(points: frozenset[tuple[str, ...]]) -> frozenset[tuple[str, ...]]:
    """下游解析时会经过的所有对象路径（消费点的全部祖先，含根 ``()``）。

    严格（strict）下游在这些对象上遇到未知字段即解析失败，
    用于判定「上游新增字段」是否触及它。
    """
    objects: set[tuple[str, ...]] = set()
    for point in points:
        for i in range(len(point) + 1):
            objects.add(point[:i])
    return frozenset(objects)
