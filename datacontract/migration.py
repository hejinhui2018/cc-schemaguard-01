"""迁移规划。

当 :mod:`datacontract.compatibility` 判定不兼容时，把每条 ERROR 级
finding 翻译成一组**有序的**迁移步骤，遵循 expand-contract 模式：

1. 生产者先做加法（加字段/双写/保持下发），对老消费者保持兼容；
2. 消费方逐个升级到新读法；
3. 回填/等待存量数据窗口；
4. 生产者再做收窄（删字段/停发旧枚举/收紧必填），这一步通常不可安全回滚；
5. 收尾与默认值语义审计。

每一步标注：

- ``actor``：影响生产者还是消费者（或两者）；
- ``rollback_safe`` / ``rollback``：能否安全回滚及原因；
- ``paths`` / ``rule_id``：可追溯到具体变更与规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .compatibility import CompatibilityReport, Finding

# 阶段序号：同一规则内与跨规则之间都按它排序
PHASE_PRODUCER_EXPAND = 10
PHASE_CONSUMER_UPGRADE = 20
PHASE_BACKFILL = 30
PHASE_PRODUCER_CONTRACT = 40
PHASE_ADVISORY = 90

PRODUCER = "producer"
CONSUMER = "consumer"
BOTH = "both"


@dataclass(frozen=True)
class MigrationStep:
    order: int
    phase: int
    actor: str
    action: str
    detail: str
    rollback_safe: bool
    rollback: str
    rule_id: str
    paths: tuple[str, ...] = ()
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "phase": self.phase,
            "actor": self.actor,
            "action": self.action,
            "detail": self.detail,
            "rollback_safe": self.rollback_safe,
            "rollback": self.rollback,
            "rule_id": self.rule_id,
            "paths": list(self.paths),
            "severity": self.severity,
        }


@dataclass
class MigrationPlan:
    steps: list[MigrationStep] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(s.severity == "error" for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "steps": [s.to_dict() for s in self.steps],
        }


def plan_migration(
    report: CompatibilityReport,
    *,
    include_advisories: bool = True,
) -> MigrationPlan:
    """根据兼容性报告生成有序迁移计划。

    完全兼容时返回空计划（或仅含 WARNING 级建议，见 include_advisories）。
    类型变更可能同时命中前向/后向两条规则（F021 与 B021），但它们描述的是
    同一个物理变更，expand-contract 方案也只有一份，因此按路径合并，避免
    生成重复步骤。
    """
    raw: list[MigrationStep] = []

    type_change_paths: dict[str, list[Finding]] = {}
    for finding in report.errors():
        if finding.rule_id in ("F021", "B021"):
            type_change_paths.setdefault(finding.path, []).append(finding)
        else:
            raw.extend(_plan_for_error(finding))

    for path in sorted(type_change_paths):
        findings = type_change_paths[path]
        rids = sorted({f.rule_id for f in findings})
        # 双向都不兼容时用合并规则号；单向则保留原规则号，均保持可追溯
        merged_rule = "F021/B021" if len(rids) == 2 else rids[0]
        representative = findings[0]
        merged = replace(
            findings[0],
            rule_id=merged_rule,
            message="；".join(dict.fromkeys(f.message for f in findings)),
            changes=tuple(ch for fnd in findings for ch in fnd.changes),
        )
        raw.extend(_plan_for_error(merged))

    if include_advisories:
        for finding in report.warnings():
            raw.extend(_plan_for_warning(finding))

    # 稳定排序：阶段 → 路径 → 规则，保证多次运行输出一致
    raw.sort(key=lambda s: (s.phase, s.paths[0] if s.paths else "", s.rule_id, s.action))
    steps = [replace(s, order=i + 1) for i, s in enumerate(raw)]
    return MigrationPlan(steps=steps)


# ---------------------------------------------------------------------------
# ERROR → 迁移步骤
# ---------------------------------------------------------------------------


def _step(
    phase: int,
    actor: str,
    action: str,
    detail: str,
    rule_id: str,
    paths: tuple[str, ...],
    rollback_safe: bool,
    rollback: str,
    severity: str = "error",
) -> MigrationStep:
    return MigrationStep(
        order=0,
        phase=phase,
        actor=actor,
        action=action,
        detail=detail,
        rollback_safe=rollback_safe,
        rollback=rollback,
        rule_id=rule_id,
        paths=paths,
        severity=severity,
    )


def _plan_for_error(finding: Finding) -> list[MigrationStep]:
    p = (finding.path,)
    rid = finding.rule_id

    if rid == "B001":  # 新增必填字段（无默认值）
        return [
            _step(
                PHASE_PRODUCER_EXPAND, PRODUCER,
                f"先以「可选 + 默认值」形式上线 {finding.path}",
                "生产者开始下发该字段，并提供契约级默认值；此时字段是纯增量，老消费者忽略即可。",
                rid, p, True,
                "回滚安全：停发该字段即恢复原状，无消费方依赖它。",
            ),
            _step(
                PHASE_CONSUMER_UPGRADE, CONSUMER,
                f"所有消费方升级为读取 {finding.path}",
                "下游升级解析逻辑，字段缺失时按默认值兜底；灰度确认全部消费方就位。",
                rid, p, True,
                "回滚安全：消费方回滚后只是不再读取新字段。",
            ),
            _step(
                PHASE_BACKFILL, PRODUCER,
                f"回填存量数据中的 {finding.path}",
                "对历史消息/存储做回填，确保回溯读取和留存窗口内的老数据也带该字段。",
                rid, p, True,
                "回填作业可重复执行、可中止；不影响在线链路。",
            ),
            _step(
                PHASE_PRODUCER_CONTRACT, PRODUCER,
                f"回填完成后将 {finding.path} 收紧为必填",
                "修改契约去掉可选与默认值并发布；此步应作为最后收窄。",
                rid, p, False,
                "回滚不安全：一旦有新消费者按必填读取，恢复“可选/缺失”会使其失败。",
            ),
        ]

    if rid in ("F002", "F004"):  # 必填字段删除 / 必填→可选
        return [
            _step(
                PHASE_PRODUCER_EXPAND, PRODUCER,
                f"将 {finding.path} 标记为可选并标注废弃，但继续下发",
                "契约先放宽、生产者不停发，给下游迁移窗口；在变更公告中给出弃用时间点。",
                rid, p, True,
                "回滚安全：恢复必填标记即可，字段数据从未中断。",
            ),
            _step(
                PHASE_CONSUMER_UPGRADE, CONSUMER,
                f"下游移除对 {finding.path} 的强依赖",
                "各消费方改为字段缺失时走兜底逻辑，确认无人再按必填访问。",
                rid, p, True,
                "回滚安全：消费方回滚后字段仍在下发，不影响读取。",
            ),
            _step(
                PHASE_PRODUCER_CONTRACT, PRODUCER,
                f"停止下发并从契约删除 {finding.path}",
                "经过完整留存窗口、确认无消费方读取后再删除；建议先开一段时间的访问监控。",
                rid, p, False,
                "回滚不安全：老版本消费方仍然读取该字段，恢复下发前它们会持续缺值。",
            ),
        ]

    if rid == "B003":  # 可选→必填
        return [
            _step(
                PHASE_PRODUCER_EXPAND, PRODUCER,
                f"生产者保证始终下发 {finding.path}",
                "在生产侧补齐该字段（缺失时填默认值），但契约暂不收紧。",
                rid, p, True,
                "回滚安全：多下发的字段对老消费者无影响。",
            ),
            _step(
                PHASE_CONSUMER_UPGRADE, CONSUMER,
                f"下游确认对 {finding.path} 的读取容忍历史缺失",
                "新消费者上线时仍需对留存窗口内可能缺失该字段的历史数据做兜底。",
                rid, p, True,
                "回滚安全：兜底逻辑保留只会更健壮。",
            ),
            _step(
                PHASE_BACKFILL, PRODUCER,
                f"回填存量数据中缺失的 {finding.path}",
                "确保历史数据在留存窗口结束前补齐字段。",
                rid, p, True,
                "回填可重复执行，可安全中止。",
            ),
            _step(
                PHASE_PRODUCER_CONTRACT, PRODUCER,
                f"将 {finding.path} 收紧为必填",
                "数据与消费方都就位后再在契约中改为必填。",
                rid, p, False,
                "回滚不安全：新消费者已按必填访问，恢复可选会导致缺值静默错误。",
            ),
        ]

    if rid in ("F010", "F012", "F014"):  # 枚举放宽：新增取值/替换/解除约束（伤老消费者）
        return [
            _step(
                PHASE_CONSUMER_UPGRADE, CONSUMER,
                f"消费者先升级对 {finding.path} 新取值的识别",
                "所有消费方发布能识别新取值（或对未知枚举值有显式兜底而非静默默认）的版本。",
                rid, p, True,
                "回滚安全：提前具备识别能力不会影响只含旧值的数据。",
            ),
            _step(
                PHASE_PRODUCER_CONTRACT, PRODUCER,
                f"消费方就位后再下发 {finding.path} 的新取值",
                "灰度放量新取值并观察消费侧错误率；确认全部消费方升级完成后再全量。",
                rid, p, False,
                "回滚不安全：已下发的数据含新取值，老消费者回溯读取仍会失败。",
            ),
        ]

    if rid in ("B011", "B012", "B013"):  # 枚举收窄：移除取值/替换/新增约束（伤新消费者读老数据）
        return [
            _step(
                PHASE_CONSUMER_UPGRADE, CONSUMER,
                f"新消费者保留对 {finding.path} 历史取值的兼容映射",
                "在新消费者中对将被移除的取值做显式映射或容错，保证留存窗口内可读。",
                rid, p, True,
                "回滚安全：兼容映射只增不减。",
            ),
            _step(
                PHASE_PRODUCER_EXPAND, PRODUCER,
                f"生产者停止产生 {finding.path} 的旧取值",
                "先在生产侧停止写旧值（新数据全部落在保留集合内），契约枚举暂不修改。",
                rid, p, True,
                "回滚安全：恢复写旧值时，尚在容忍期的新消费者仍能识别。",
            ),
            _step(
                PHASE_BACKFILL, PRODUCER,
                f"回填存量数据中 {finding.path} 的旧取值",
                "把历史数据中的旧值映射到保留取值；等待至少一个完整数据留存窗口。",
                rid, p, True,
                "回填作业可重复执行、可中止。",
            ),
            _step(
                PHASE_PRODUCER_CONTRACT, PRODUCER,
                f"从契约枚举中移除 {finding.path} 的旧取值",
                "存量窗口过去、消费者兼容代码确认可删后再收窄契约。",
                rid, p, False,
                "回滚不安全：一旦消费者删掉兼容映射，旧值数据将再次无法解析。",
            ),
        ]

    if rid in ("F021", "B021", "F021/B021"):  # 类型变化（可能双向命中）
        return [
            _step(
                PHASE_PRODUCER_EXPAND, PRODUCER,
                f"为 {finding.path} 引入新字段并双写（推荐 *_v2 命名）",
                "不要原地改类型：新增一个新类型的字段，生产者同时写老字段与新字段。",
                rid, p, True,
                "回滚安全：新字段是纯增量，停写即恢复。",
            ),
            _step(
                PHASE_CONSUMER_UPGRADE, CONSUMER,
                f"消费者迁移到读取 {finding.path} 的新字段",
                "下游优先读新字段、缺失时回退老字段；逐方灰度切换。",
                rid, p, True,
                "回滚安全：回退逻辑仍指向老字段。",
            ),
            _step(
                PHASE_BACKFILL, BOTH,
                "核对存量数据在两种类型表示下的一致性",
                f"重点检查 {finding.path} 的精度/格式/取整规则，确认双写期间两份表示语义等价。",
                rid, p, True,
                "核对不修改线上数据，可随时停止。",
            ),
            _step(
                PHASE_PRODUCER_CONTRACT, PRODUCER,
                f"确认无人读取后删除 {finding.path} 的老字段",
                "经过监控确认老字段零访问，再停止双写并从契约删除老字段。",
                rid, p, False,
                "回滚不安全：未迁移完的消费者会直接失去该数据。",
            ),
        ]

    # 兜底：未知规则的通用 expand-contract
    return [
        _step(
            PHASE_CONSUMER_UPGRADE, BOTH,
            f"针对 {finding.path} 的不兼容变更制定双写/兼容窗口",
            finding.message,
            rid, p, True,
            "扩展阶段（只做加法）可安全回滚。",
        ),
        _step(
            PHASE_PRODUCER_CONTRACT, PRODUCER,
            f"消费方全部迁移后再收窄 {finding.path}",
            "删除/停发/收紧类操作必须放在最后。",
            rid, p, False,
            "收窄操作通常不可安全回滚。",
        ),
    ]


# ---------------------------------------------------------------------------
# WARNING → 建议步骤（不阻断）
# ---------------------------------------------------------------------------


def _plan_for_warning(finding: Finding) -> list[MigrationStep]:
    p = (finding.path,)
    rid = finding.rule_id

    if rid in ("F001", "B002"):
        return [
            _step(
                PHASE_ADVISORY, CONSUMER,
                f"确认消费者对未知字段采取宽容解析：{finding.path}",
                "建议统一「忽略未知字段」的解析策略，避免严格模式在字段增删时被击穿。",
                rid, p, True,
                "解析策略调整可独立回滚。",
                severity="warning",
            )
        ]

    if rid in ("F030", "F031", "F032", "B030", "B031", "B032"):
        return [
            _step(
                PHASE_ADVISORY, BOTH,
                f"审计 {finding.path} 默认值语义的一致性",
                finding.message
                + "；排查下游是否硬编码了各自的默认值，避免契约默认值与消费方本地默认值分叉。",
                rid, p, True,
                "审计不改变线上行为。",
                severity="warning",
            )
        ]

    if rid in ("F040", "B040"):
        return [
            _step(
                PHASE_ADVISORY, CONSUMER,
                f"确认额外属性策略与消费者解析行为一致：{finding.path}",
                finding.message,
                rid, p, True,
                "解析策略调整可独立回滚。",
                severity="warning",
            )
        ]

    if rid in ("F020", "B020"):
        return [
            _step(
                PHASE_ADVISORY, BOTH,
                f"确认数值宽度变化在 {finding.path} 上不丢精度",
                finding.message,
                rid, p, True,
                "核对不改变线上行为。",
                severity="warning",
            )
        ]

    return []
