"""兼容性判定引擎。

回答三个问题：

- FORWARD（前向兼容）：**按老契约写的消费者**能否读取新契约产生的数据；
- BACKWARD（后向兼容）：**按新契约写的消费者**能否读取历史遗留的老数据；
- FULL：两者同时成立。

每条不兼容/风险结论都是一个 :class:`Finding`，带规则 ID、严重级别、
数据路径以及**导致该结论的具体变更**，可逐条追溯。

判定原则：

- ERROR = 在该方向上存在必然（或高度可能）的解析失败/静默错值；
- WARNING = 不阻断，但有需要人工确认的语义风险（典型如默认值变化）；
- 完全良性的变更（如新增可选字段对后向兼容）不产生 finding。

规则表（F=前向，B=后向）详见 README「兼容性规则」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .diff import Change, ChangeKind, diff_contracts
from .model import Contract


class Direction(str, Enum):
    FORWARD = "forward"
    BACKWARD = "backward"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


# 一条规则的返回值：None 表示该变更在该方向上良性（不产生 finding）
_Verdict = tuple[Severity, str, str] | None  # (severity, rule_id, message)


@dataclass(frozen=True)
class Finding:
    severity: Severity
    direction: Direction
    rule_id: str
    path: str
    message: str
    changes: tuple[Change, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "direction": self.direction.value,
            "rule_id": self.rule_id,
            "path": self.path,
            "message": self.message,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class CompatibilityReport:
    old_version: str | None
    new_version: str | None
    changes: list[Change] = field(default_factory=list)
    forward_findings: list[Finding] = field(default_factory=list)
    backward_findings: list[Finding] = field(default_factory=list)

    @property
    def forward_compatible(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.forward_findings)

    @property
    def backward_compatible(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.backward_findings)

    @property
    def fully_compatible(self) -> bool:
        return self.forward_compatible and self.backward_compatible

    def findings(self, direction: Direction) -> list[Finding]:
        return (
            self.forward_findings
            if direction is Direction.FORWARD
            else self.backward_findings
        )

    def errors(self, direction: Direction | None = None) -> list[Finding]:
        return [f for f in self._iter(direction) if f.severity is Severity.ERROR]

    def warnings(self, direction: Direction | None = None) -> list[Finding]:
        return [f for f in self._iter(direction) if f.severity is Severity.WARNING]

    def _iter(self, direction: Direction | None):
        dirs = (
            (Direction.FORWARD, Direction.BACKWARD)
            if direction is None
            else (direction,)
        )
        for d in dirs:
            yield from self.findings(d)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "forward_compatible": self.forward_compatible,
            "backward_compatible": self.backward_compatible,
            "fully_compatible": self.fully_compatible,
            "changes": [c.to_dict() for c in self.changes],
            "forward": [f.to_dict() for f in self.forward_findings],
            "backward": [f.to_dict() for f in self.backward_findings],
        }


def check_compatibility(
    old: Contract,
    new: Contract,
    *,
    changes: list[Change] | None = None,
) -> CompatibilityReport:
    """判定 old → new 的兼容性，每条结论都可追溯到具体变更。"""
    changes = changes if changes is not None else diff_contracts(old, new)
    report = CompatibilityReport(
        old_version=old.version,
        new_version=new.version,
        changes=changes,
    )

    for direction in (Direction.FORWARD, Direction.BACKWARD):
        bucket = (
            report.forward_findings
            if direction is Direction.FORWARD
            else report.backward_findings
        )
        for change in changes:
            # $defs 下的孤岛定义变更不经过线上数据结构；被引用的定义会
            # 在真实数据路径下再出现一次，所以这里跳过以免误报。
            if change.path.startswith("$defs."):
                continue
            verdict = _RULES.dispatch(change, direction)
            if verdict is None:
                continue
            severity, rule_id, message = verdict
            bucket.append(
                Finding(
                    severity=severity,
                    direction=direction,
                    rule_id=rule_id,
                    path=change.path,
                    message=message,
                    changes=(change,),
                )
            )
        bucket.sort(key=lambda f: (f.path, f.rule_id))
    return report


# ---------------------------------------------------------------------------
# 规则矩阵：ChangeKind × Direction → verdict
# ---------------------------------------------------------------------------


class _RuleMatrix:
    def __init__(self) -> None:
        # (kind, direction) -> callable(Change) -> _Verdict
        self._table: dict[
            tuple[ChangeKind, Direction], Callable[[Change], _Verdict]
        ] = {}

    def rule(self, kind: ChangeKind, direction: Direction, severity: Severity, rule_id: str):
        """注册一条固定严重级别的规则；函数返回 None 表示该变更在此方向良性。"""

        def deco(fn: Callable[[Change], str | None]):
            def wrapped(c: Change) -> _Verdict:
                message = fn(c)
                if message is None:
                    return None
                return severity, rule_id, message

            self._table[(kind, direction)] = wrapped
            return fn

        return deco

    def rule_dynamic(self, kind: ChangeKind, direction: Direction):
        """注册严重级别随变更内容变化的规则（如 integer↔number）。"""

        def deco(fn: Callable[[Change], _Verdict]):
            self._table[(kind, direction)] = fn
            return fn

        return deco

    def dispatch(self, change: Change, direction: Direction) -> _Verdict:
        fn = self._table.get((change.kind, direction))
        if fn is None:
            return None
        return fn(change)


_RULES = _RuleMatrix()
_F = Direction.FORWARD
_B = Direction.BACKWARD
_E = Severity.ERROR
_W = Severity.WARNING


def _meta(change: Change, key: str, default: Any = None) -> Any:
    return (change.meta or {}).get(key, default)


# ---- 字段增删 ----------------------------------------------------------------


@_RULES.rule(ChangeKind.FIELD_ADDED, _F, _W, "F001")
def _(c: Change) -> str | None:
    # 老消费者通常忽略多余字段；仅“严格拒绝未知字段”的实现有风险。
    return f"新数据会多出字段 {c.field!r}；若老消费者严格拒绝未知字段，需先升级消费者"


@_RULES.rule(ChangeKind.FIELD_ADDED, _B, _E, "B001")
def _(c: Change) -> str | None:
    if _meta(c, "optional") or _meta(c, "has_default"):
        # 可选 / 有契约级默认值：新消费者能处理老数据的缺失
        return None
    return (
        f"新增字段 {c.field!r} 为必填且无默认值，历史数据中不存在该字段，"
        "新消费者读取老数据必然缺值"
    )


@_RULES.rule(ChangeKind.FIELD_REMOVED, _F, _E, "F002")
def _(c: Change) -> str | None:
    if _meta(c, "optional"):
        # 老消费者按契约本就必须能处理该字段缺失，生产者停发是安全的
        return None
    return f"必填字段 {c.field!r} 被删除，新数据不再提供；老消费者会缺字段或静默落入默认值"


@_RULES.rule(ChangeKind.FIELD_REMOVED, _B, _W, "B002")
def _(c: Change) -> str | None:
    return f"老数据可能仍携带已删除字段 {c.field!r}；严格解析的新消费者需忽略未知字段"


# ---- 可选性变化 --------------------------------------------------------------


@_RULES.rule(ChangeKind.FIELD_MADE_REQUIRED, _F, _W, "F003")
def _(c: Change) -> str | None:
    # 新数据只会更完整，老消费者读取不受影响；不报错，也无需打扰（无 finding）。
    return None


@_RULES.rule(ChangeKind.FIELD_MADE_REQUIRED, _B, _E, "B003")
def _(c: Change) -> str | None:
    return f"字段 {c.field!r} 由可选变为必填，历史数据可能缺失，新消费者会读取失败或得到错误默认值"


@_RULES.rule(ChangeKind.FIELD_MADE_OPTIONAL, _F, _E, "F004")
def _(c: Change) -> str | None:
    return (
        f"字段 {c.field!r} 由必填变为可选，生产者可能停止下发；"
        "老消费者按必填访问会出现空值/静默默认值"
    )


@_RULES.rule(ChangeKind.FIELD_MADE_OPTIONAL, _B, _W, "B004")
def _(c: Change) -> str | None:
    # 新消费者已按可选处理缺失，老数据反而更完整，安全。
    return None


# ---- 枚举 --------------------------------------------------------------------


@_RULES.rule(ChangeKind.ENUM_EXTENDED, _F, _E, "F010")
def _(c: Change) -> str | None:
    return f"枚举新增取值 {c.new!r}，新数据可能携带老消费者无法识别的值，易落入兜底默认值"


@_RULES.rule(ChangeKind.ENUM_EXTENDED, _B, _W, "B010")
def _(c: Change) -> str | None:
    return None  # 放宽：老数据的所有取值新消费者都认识


@_RULES.rule(ChangeKind.ENUM_REDUCED, _F, _W, "F011")
def _(c: Change) -> str | None:
    return None  # 收窄：新数据只会取老消费者认识的值


@_RULES.rule(ChangeKind.ENUM_REDUCED, _B, _E, "B011")
def _(c: Change) -> str | None:
    return f"枚举移除取值 {c.old!r}，历史数据中可能仍存在，新消费者无法识别"


@_RULES.rule(ChangeKind.ENUM_REPLACED, _F, _E, "F012")
def _(c: Change) -> str | None:
    return f"枚举取值被替换，新增成员 {c.new!r} 对老消费者不可识别"


@_RULES.rule(ChangeKind.ENUM_REPLACED, _B, _E, "B012")
def _(c: Change) -> str | None:
    return f"枚举取值被替换，历史数据中的 {c.old!r} 对新消费者不可识别"


@_RULES.rule(ChangeKind.ENUM_CONSTRAINED, _F, _W, "F013")
def _(c: Change) -> str | None:
    return None  # 取值空间收窄，老消费者按自由标量读取无影响


@_RULES.rule(ChangeKind.ENUM_CONSTRAINED, _B, _E, "B013")
def _(c: Change) -> str | None:
    return "新增枚举约束不覆盖历史数据中可能出现的任意取值，新消费者会遇到未知枚举值"


@_RULES.rule(ChangeKind.ENUM_UNCONSTRAINED, _F, _E, "F014")
def _(c: Change) -> str | None:
    return "枚举约束被移除，新数据可能出现老消费者枚举集合之外的任意取值"


@_RULES.rule(ChangeKind.ENUM_UNCONSTRAINED, _B, _W, "B014")
def _(c: Change) -> str | None:
    return None  # 新消费者接受任意取值，老数据均能读取


# ---- 类型变化 ----------------------------------------------------------------

# 标量特殊组合：None 单元格 = 该方向安全（不产生 finding）
# 未列出的任意标量组合：两个方向均按 ERROR 处理。
_SCALAR_SPECIAL: dict[tuple[str, str], dict[Direction, tuple[Severity, str, str] | None]] = {
    ("integer", "number"): {
        # 新数据可能出现小数，老消费者按整数处理有丢精度风险
        _F: (_W, "F020", "integer 放宽为 number：新数据可能出现小数，老消费者按 integer 处理可能丢精度"),
        # 老数据是整数，对 number 消费者天然合法
        _B: None,
    },
    ("number", "integer"): {
        # 新数据是整数，老 number 消费者读取完全合法
        _F: None,
        # 历史 number 数据可能含小数，新消费者按整数解析可能失败/丢精度
        _B: (_W, "B020", "number 收窄为 integer：历史数据可能含小数，新消费者解析可能失败或丢精度"),
    },
}


@_RULES.rule_dynamic(ChangeKind.TYPE_CHANGED, _F)
def _(c: Change) -> _Verdict:
    return _type_verdict(c, _F, "F021")


@_RULES.rule_dynamic(ChangeKind.TYPE_CHANGED, _B)
def _(c: Change) -> _Verdict:
    return _type_verdict(c, _B, "B021")


def _type_verdict(c: Change, direction: Direction, fallback_rule: str) -> _Verdict:
    if _meta(c, "category") == "scalar":
        special = _SCALAR_SPECIAL.get((str(c.old), str(c.new)))
        if special is not None:
            return special[direction]
    if direction is _F:
        return _E, fallback_rule, f"类型由 {c.old} 变为 {c.new}，老消费者无法按原类型解析新数据"
    return _E, fallback_rule, f"类型由 {c.old} 变为 {c.new}，新消费者无法按新类型解析历史数据"


# ---- 默认值 ------------------------------------------------------------------


@_RULES.rule(ChangeKind.DEFAULT_ADDED, _F, _W, "F030")
def _(c: Change) -> str | None:
    return "生产者新增默认值；确认新兜底语义与老消费者内置的默认逻辑一致"


@_RULES.rule(ChangeKind.DEFAULT_ADDED, _B, _W, "B030")
def _(c: Change) -> str | None:
    return "新增契约级默认值可弥补历史数据缺失，但要确认该默认值对老数据在业务上成立"


@_RULES.rule(ChangeKind.DEFAULT_REMOVED, _F, _W, "F031")
def _(c: Change) -> str | None:
    return "生产者移除默认值；老消费者若依赖自身旧默认逻辑，注意两侧默认语义分叉"


@_RULES.rule(ChangeKind.DEFAULT_REMOVED, _B, _W, "B031")
def _(c: Change) -> str | None:
    return "默认值被移除，新消费者读取缺字段的历史数据时不再有契约级兜底，可能得到空值"


@_RULES.rule(ChangeKind.DEFAULT_CHANGED, _F, _W, "F032")
def _(c: Change) -> str | None:
    return f"默认值变化（{c.old!r} → {c.new!r}），缺失字段时新老数据语义不一致，存在静默错值风险"


@_RULES.rule(ChangeKind.DEFAULT_CHANGED, _B, _W, "B032")
def _(c: Change) -> str | None:
    return f"默认值变化（{c.old!r} → {c.new!r}），新消费者对历史缺失数据套用的默认值与当时语义不同"


# ---- additionalProperties ----------------------------------------------------


@_RULES.rule(ChangeKind.ADDITIONAL_PROPERTIES_CHANGED, _F, _W, "F040")
def _(c: Change) -> str | None:
    if c.new is False:
        return "对象不再允许额外属性，生产者可能停发老消费者依赖的多余字段"
    return "对象开始允许额外属性，严格解析的老消费者需确认能忽略未知字段"


@_RULES.rule(ChangeKind.ADDITIONAL_PROPERTIES_CHANGED, _B, _W, "B040")
def _(c: Change) -> str | None:
    if c.new is False:
        return "对象不再允许额外属性，历史数据中的多余字段会被严格的新消费者拒绝"
    return "对象开始允许额外属性，新消费者需容忍历史数据中的未知字段"
