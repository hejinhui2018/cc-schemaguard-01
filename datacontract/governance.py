"""多下游治理评估。

把一次上游契约变更（old → new）投影到**每个下游实际消费的数据结构**上，
回答治理组的四个问题：

1. **按下游给结论**：每个下游按当前消费方式能否读取新契约产生的数据；
   不能时，是上游的哪些具体变更导致的（追溯到 :class:`Change` 与规则 ID）；
2. **影响面**：上游多出来的字段不影响不读它的下游；下游用到的字段
   被改掉一定会被发现；
3. **迁移落到下游**：建议步骤按「上游（producer）/ 具体下游
   （``consumer:<名字>``）」分工，并保留每一步的可回滚性标注；
4. **放行口径**：``PASS`` / ``PASS_WITH_WARNINGS`` / ``BLOCKED`` 三档，
   区分「只影响个别非关键下游」与「打挂仍按老方式消费的关键下游」，
   且每条阻断原因都能追溯到具体下游与具体差异。

评估语义：下游画像描述的是它**当前**（基于上游老契约）的消费方式，
本模块回答的是「这次上游变更会不会破坏它」——即把 old → new 的
结构化差异投影到各下游的消费点上，逐条套用前向（老消费者读新数据）
规则。上游自身的后向兼容（新代码读历史数据）与下游无关，单独计入
放行口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Sequence

from .compatibility import (
    CompatibilityReport,
    Direction,
    Finding,
    Severity,
    check_compatibility,
    judge_change,
)
from .consumer import (
    ConsumerProfile,
    check_unique_names,
    parsed_object_paths,
    parse_change_path,
    path_affects,
)
from .diff import Change, ChangeKind, diff_contracts
from .migration import (
    BOTH,
    PHASE_PRODUCER_CONTRACT,
    PRODUCER,
    MigrationStep,
    steps_for_error,
    steps_for_warning,
)
from .model import Contract

# 放行口径阈值（--fail-on）
FAIL_CRITICAL = "critical"  # 默认：关键下游受损（或上游自身后向不兼容）才阻断
FAIL_ANY = "any"            # 任何下游受损都阻断
FAIL_NEVER = "never"        # 只出报告，永不阻断


class GateVerdict(str, Enum):
    """治理放行口径。"""

    PASS = "pass"                              # 全部下游按当前消费方式均可读新数据
    PASS_WITH_WARNINGS = "pass_with_warnings"  # 仅非关键下游存在阻断项
    BLOCKED = "blocked"                        # 关键下游受损，或上游自身后向不兼容


@dataclass(frozen=True)
class ConsumerAssessment:
    """单个下游的评估结论。"""

    name: str
    critical: bool
    strict: bool
    findings: tuple[Finding, ...] = ()
    # 投影命中该下游的全部变更（含判定为良性的），保证影响面可追溯
    relevant_changes: tuple[Change, ...] = ()

    @property
    def compatible(self) -> bool:
        """按当前消费方式能否正确读取新数据（无 ERROR 即视为能）。"""
        return not any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "critical": self.critical,
            "strict": self.strict,
            "compatible": self.compatible,
            "findings": [f.to_dict() for f in self.findings],
            "relevant_changes": [c.to_dict() for c in self.relevant_changes],
        }


@dataclass
class GovernancePlan:
    """按执行者拆分的迁移计划。

    ``steps`` 中 ``actor`` 为 ``producer`` / ``both`` 的是上游要做的；
    ``consumer:<下游名>`` 是某个下游自己要做的。同一物理变更服务多个
    下游时，上游步骤只保留一份，并在 detail 中标注受影响下游名单；
    收窄（contract）阶段的步骤会显式列出「必须等哪些下游全部迁移完成」。
    """

    steps: list[MigrationStep] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(s.severity == "error" for s in self.steps)

    @property
    def producer_steps(self) -> list[MigrationStep]:
        return [s for s in self.steps if s.actor in (PRODUCER, BOTH)]

    @property
    def consumer_steps(self) -> list[MigrationStep]:
        return [s for s in self.steps if s.actor.startswith("consumer:")]

    def steps_for(self, consumer_name: str) -> list[MigrationStep]:
        """某个下游自己要执行的步骤。"""
        return [s for s in self.steps if s.actor == f"consumer:{consumer_name}"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class GovernanceReport:
    """一次多下游评估的完整结论。"""

    old_version: str | None
    new_version: str | None
    producer: CompatibilityReport          # 上游自身 old → new 的全局兼容性
    consumers: list[ConsumerAssessment] = field(default_factory=list)
    plan: GovernancePlan = field(default_factory=GovernancePlan)

    @property
    def blocking_reasons(self) -> list[str]:
        """构成 BLOCKED 的全部原因（追溯到具体下游 / 具体差异）。"""
        reasons: list[str] = []
        for f in self.producer.errors(Direction.BACKWARD):
            reasons.append(
                f"上游自身后向不兼容: {f.path} [{f.rule_id}] {f.message}"
            )
        for a in self.consumers:
            if not a.critical:
                continue
            for f in a.errors:
                reasons.append(
                    f"关键下游 {a.name} 无法读取新数据: {f.path} [{f.rule_id}] {f.message}"
                )
        return reasons

    @property
    def warning_reasons(self) -> list[str]:
        """不构成 BLOCKED 但需要知会的事项（非关键下游受损等）。"""
        reasons: list[str] = []
        if not self.consumers:
            for f in self.producer.errors(Direction.FORWARD):
                reasons.append(
                    f"前向不兼容: {f.path} [{f.rule_id}] {f.message}"
                    "（未提供下游画像，无法评估影响面）"
                )
        for a in self.consumers:
            if a.critical:
                continue
            for f in a.errors:
                reasons.append(
                    f"非关键下游 {a.name} 无法读取新数据: {f.path} [{f.rule_id}] {f.message}"
                )
        return reasons

    @property
    def verdict(self) -> GateVerdict:
        if self.blocking_reasons:
            return GateVerdict.BLOCKED
        if self.warning_reasons:
            return GateVerdict.PASS_WITH_WARNINGS
        return GateVerdict.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "verdict": self.verdict.value,
            "blocking_reasons": self.blocking_reasons,
            "warning_reasons": self.warning_reasons,
            "producer": self.producer.to_dict(),
            "consumers": [a.to_dict() for a in self.consumers],
            "plan": self.plan.to_dict(),
        }


@dataclass
class GovernanceEvaluation:
    """评估结果 + 放行阈值，供 CI 判定退出码。"""

    report: GovernanceReport
    fail_on: str = FAIL_CRITICAL

    @property
    def passed(self) -> bool:
        if self.fail_on == FAIL_NEVER:
            return True
        verdict = self.report.verdict
        if verdict is GateVerdict.BLOCKED:
            return False
        if self.fail_on == FAIL_ANY and verdict is GateVerdict.PASS_WITH_WARNINGS:
            return False
        return True

    def exit_code(self) -> int:
        """与 :mod:`datacontract.cli` 的 EXIT_OK / EXIT_INCOMPATIBLE 一致。"""
        return 0 if self.passed else 1

    def to_dict(self) -> dict[str, Any]:
        payload = self.report.to_dict()
        payload["passed"] = self.passed
        payload["fail_on"] = self.fail_on
        return payload


# ---------------------------------------------------------------------------
# 评估主流程
# ---------------------------------------------------------------------------


def evaluate_governance(
    old: Contract,
    new: Contract,
    consumers: Sequence[ConsumerProfile] = (),
    *,
    fail_on: str = FAIL_CRITICAL,
) -> GovernanceEvaluation:
    """对一次上游发版做多下游治理评估。

    - ``old`` / ``new``：上游契约的旧版与新版；
    - ``consumers``：各下游的消费画像（可为空，此时只评估上游自身兼容性，
      前向不兼容会以「影响面未知」列入 warning_reasons）；
    - ``fail_on``：放行阈值，见 ``FAIL_*`` 常量。
    """
    check_unique_names(list(consumers))

    changes = diff_contracts(old, new)
    producer_report = check_compatibility(old, new, changes=changes)

    assessments = [
        _assess_consumer(profile, changes) for profile in consumers
    ]
    plan = _build_governance_plan(assessments, producer_report)

    report = GovernanceReport(
        old_version=old.version,
        new_version=new.version,
        producer=producer_report,
        consumers=assessments,
        plan=plan,
    )
    return GovernanceEvaluation(report=report, fail_on=fail_on)


def _assess_consumer(
    profile: ConsumerProfile, changes: list[Change]
) -> ConsumerAssessment:
    """把上游变更投影到单个下游的消费点上，逐条套用前向规则。"""
    points = profile.consumption_points()
    objects = parsed_object_paths(points)

    relevant: list[Change] = []
    findings: list[Finding] = []
    for change in changes:
        # $defs 下的孤岛定义变更不经过线上数据结构（与全局评估口径一致）
        if change.path.startswith("$defs."):
            continue
        segs = parse_change_path(change.path)

        if path_affects(segs, points):
            relevant.append(change)
        elif (
            change.kind is ChangeKind.FIELD_ADDED
            and profile.strict
            and segs[:-1] in objects
        ):
            # 严格下游：其解析范围内的对象新增未知字段即解析失败
            relevant.append(change)
        else:
            continue  # 与该下游无关的变更

        verdict = judge_change(change, Direction.FORWARD)
        if verdict is None:
            continue
        severity, rule_id, message = verdict
        if change.kind is ChangeKind.FIELD_ADDED and profile.strict:
            # F001 的「若严格拒绝未知字段」对该下游成立：提示升级为阻断
            severity = Severity.ERROR
            message = (
                f"新数据会多出字段 {change.field!r}，下游 {profile.name} 声明严格解析"
                "（strict=true），未知字段将导致其解析失败"
            )
        findings.append(
            Finding(
                severity=severity,
                direction=Direction.FORWARD,
                rule_id=rule_id,
                path=change.path,
                message=message,
                changes=(change,),
            )
        )

    findings.sort(key=lambda f: (f.path, f.rule_id))
    return ConsumerAssessment(
        name=profile.name,
        critical=profile.critical,
        strict=profile.strict,
        findings=tuple(findings),
        relevant_changes=tuple(relevant),
    )


def _build_governance_plan(
    assessments: list[ConsumerAssessment],
    producer_report: CompatibilityReport,
) -> GovernancePlan:
    """把各下游的 findings 翻译成按执行者拆分的有序步骤。

    - 消费侧步骤：每个受影响下游各一份，actor 为 ``consumer:<名字>``；
    - 上游步骤：同一物理变更只保留一份，detail 标注受影响下游名单；
      收窄（contract）阶段额外标注「必须等哪些下游全部迁移完成」；
    - 类型变化在下游侧（F021）与上游自身侧（B021）是同一物理变更，
      按路径合并成一套步骤（与 :func:`plan_migration` 的合并口径一致）；
    - 上游自身的后向兼容修复（新代码读历史数据）也纳入，detail 单独标注。
    """
    raw: list[MigrationStep] = []
    # 上游步骤去重池：(rule_id, paths, phase, action) -> [step, {下游名}]
    pool: dict[tuple[Any, ...], list[Any]] = {}
    # 类型变化按路径归集：path -> {"findings": [...], "consumers": {下游名}}
    type_changes: dict[str, dict[str, Any]] = {}

    def absorb_producer(step: MigrationStep, consumer_name: str | None) -> None:
        key = (step.rule_id, step.paths, step.phase, step.action)
        if key in pool:
            if consumer_name:
                pool[key][1].add(consumer_name)
        else:
            pool[key] = [step, {consumer_name} if consumer_name else set()]

    def collect_type_change(finding: Finding, consumer_name: str | None) -> None:
        entry = type_changes.setdefault(
            finding.path, {"findings": [], "consumers": {}}
        )
        entry["findings"].append(finding)
        if consumer_name:
            entry["consumers"][consumer_name] = finding

    def emit(finding: Finding, consumer_name: str | None) -> None:
        """一条 ERROR finding → 步骤（类型变化先归集，其余直接翻译）。"""
        if finding.rule_id in ("F021", "B021"):
            collect_type_change(finding, consumer_name)
            return
        for step in steps_for_error(finding, consumer=consumer_name):
            if step.actor in (PRODUCER, BOTH):
                absorb_producer(step, consumer_name)
            else:
                raw.append(step)

    for assessment in assessments:
        for finding in assessment.errors:
            emit(finding, assessment.name)
        for finding in assessment.warnings:
            raw.extend(steps_for_warning(finding, consumer=assessment.name))

    # 上游自身后向不兼容的修复（新代码读历史数据），与下游无关
    for finding in producer_report.errors(Direction.BACKWARD):
        if finding.rule_id in ("F021", "B021"):
            collect_type_change(finding, None)
            continue
        for step in steps_for_error(finding):
            step = replace(
                step, detail=step.detail + "（上游自身数据资产修复，与下游无关）"
            )
            if step.actor in (PRODUCER, BOTH):
                absorb_producer(step, None)
            else:
                raw.append(step)

    # 类型变化：同一路径的 F021（下游读新数据）与 B021（上游读历史数据）
    # 是同一物理变更，合并出一套 expand-contract 步骤
    for path in sorted(type_changes):
        entry = type_changes[path]
        findings: list[Finding] = entry["findings"]
        consumers: dict[str, Finding] = entry["consumers"]
        rule_ids = {f.rule_id for f in findings}
        merged_rule = "F021/B021" if rule_ids == {"F021", "B021"} else sorted(rule_ids)[0]
        merged = replace(
            findings[0],
            rule_id=merged_rule,
            message="；".join(dict.fromkeys(f.message for f in findings)),
            changes=tuple(ch for f in findings for ch in f.changes),
        )
        involves_producer_data = "B021" in rule_ids
        for step in steps_for_error(merged):
            if step.actor in (PRODUCER, BOTH):
                absorb_type_step(step, consumers, involves_producer_data, raw)
            else:
                # 消费侧步骤：每个受影响下游各一份
                for name in sorted(consumers):
                    raw.append(
                        replace(
                            step,
                            actor=f"consumer:{name}",
                            action=f"下游 {name} 迁移到读取 {path} 的新字段",
                        )
                    )
                if not consumers:
                    raw.append(step)  # 仅上游自身涉及：保留泛称步骤

    for step, names in pool.values():
        detail = step.detail
        if names:
            audience = "、".join(sorted(names))
            detail += f"（受影响下游：{audience}）"
            if step.phase == PHASE_PRODUCER_CONTRACT:
                detail += f"；必须等 {audience} 全部完成迁移后才可执行"
        raw.append(replace(step, detail=detail))

    # 稳定排序：阶段 → 路径 → 执行者 → 规则，保证多次运行输出一致
    raw.sort(
        key=lambda s: (s.phase, s.paths[0] if s.paths else "", s.actor, s.rule_id, s.action)
    )
    steps = [replace(s, order=i + 1) for i, s in enumerate(raw)]
    return GovernancePlan(steps=steps)


def absorb_type_step(
    step: MigrationStep,
    consumers: dict[str, Finding],
    involves_producer_data: bool,
    raw: list[MigrationStep],
) -> None:
    """类型变化的 producer/both 步骤：标注受影响下游与是否涉及上游自身。"""
    notes: list[str] = []
    if consumers:
        notes.append("受影响下游：" + "、".join(sorted(consumers)))
    if involves_producer_data:
        notes.append("同时影响上游自身读取历史数据")
    detail = step.detail + ("（" + "；".join(notes) + "）" if notes else "")
    if step.phase == PHASE_PRODUCER_CONTRACT and consumers:
        detail += f"；必须等 {'、'.join(sorted(consumers))} 全部完成迁移后才可执行"
    raw.append(replace(step, detail=detail))
