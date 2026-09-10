"""CI 集成入口。

两种用法：

1. 命令行::

       python -m datacontract check old.json new.json
       python -m datacontract check old.json new.json --format json --fail-on error
       python -m datacontract evaluate old.json new.json --consumer payment.json
       python -m datacontract evaluate old.json new.json --consumers consumers/ --fail-on any

   退出码：
       0  放行（按 --fail-on 阈值没有命中）
       1  阻断（check：存在不兼容；evaluate：命中治理放行口径）
       2  用法错误或契约/下游画像本身非法

2. 可编程::

       from datacontract import evaluate_paths, evaluate_governance
       result = evaluate_paths("old.json", "new.json")
       if not result["fully_compatible"]:
           ...  # 阻断流水线
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .compatibility import (
    CompatibilityReport,
    Direction,
    Severity,
    check_compatibility,
)
from .consumer import ConsumerError, parse_consumer_file
from .diff import diff_contracts
from .governance import (
    FAIL_ANY,
    FAIL_CRITICAL,
    GateVerdict,
    GovernanceEvaluation,
    evaluate_governance,
)
from .migration import MigrationPlan, plan_migration
from .parser import Contract, ContractError, parse_file

__version__ = "0.2.0"

EXIT_OK = 0
EXIT_INCOMPATIBLE = 1
EXIT_USAGE = 2

FAIL_NEVER = "never"
FAIL_ERROR = "error"
FAIL_WARNING = "warning"


@dataclass
class Evaluation:
    report: CompatibilityReport
    plan: MigrationPlan
    fail_on: str = FAIL_ERROR

    @property
    def passed(self) -> bool:
        if self.fail_on == FAIL_NEVER:
            return True
        if self.report.errors():
            return False
        if self.fail_on == FAIL_WARNING and self.report.warnings():
            return False
        return True

    def exit_code(self) -> int:
        return EXIT_OK if self.passed else EXIT_INCOMPATIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "fail_on": self.fail_on,
            "compatibility": self.report.to_dict(),
            "migration": self.plan.to_dict(),
        }


def evaluate(old: Contract, new: Contract, *, fail_on: str = FAIL_ERROR) -> Evaluation:
    """对两份已解析契约执行 差异→兼容性→迁移规划 全流程。"""
    changes = diff_contracts(old, new)
    report = check_compatibility(old, new, changes=changes)
    plan = plan_migration(report)
    return Evaluation(report=report, plan=plan, fail_on=fail_on)


def evaluate_paths(old_path: str | Path, new_path: str | Path, **kwargs: Any) -> Evaluation:
    """文件路径版本的 :func:`evaluate`。"""
    old = parse_file(old_path)
    new = parse_file(new_path)
    return evaluate(old, new, **kwargs)


def evaluate_governance_paths(
    old_path: str | Path,
    new_path: str | Path,
    consumer_paths: Sequence[str | Path] = (),
    **kwargs: Any,
) -> GovernanceEvaluation:
    """文件路径版本的 :func:`evaluate_governance`（多下游治理评估）。"""
    old = parse_file(old_path)
    new = parse_file(new_path)
    consumers = [parse_consumer_file(p) for p in consumer_paths]
    return evaluate_governance(old, new, consumers, **kwargs)


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------


def _render_human(ev: Evaluation) -> str:
    r = ev.report
    lines: list[str] = []
    lines.append(
        f"数据契约兼容性检查：{r.old_version or '(无版本号)'} → {r.new_version or '(无版本号)'}"
    )
    lines.append(f"  变更总数: {len(r.changes)}")
    for label, ok, findings in (
        ("前向兼容（老消费者读新数据）", r.forward_compatible, r.forward_findings),
        ("后向兼容（新消费者读老数据）", r.backward_compatible, r.backward_findings),
    ):
        errors = [f for f in findings if f.severity is Severity.ERROR]
        warns = [f for f in findings if f.severity is Severity.WARNING]
        verdict = "✓ 通过" if not errors else "✗ 不兼容"
        lines.append(f"  {label}: {verdict}（{len(errors)} 个阻断项, {len(warns)} 个警告）")

    for direction, findings in (
        (Direction.FORWARD, r.forward_findings),
        (Direction.BACKWARD, r.backward_findings),
    ):
        if not findings:
            continue
        tag = "前向" if direction is Direction.FORWARD else "后向"
        lines.append("")
        lines.append(f"[{tag}] 明细")
        for f in findings:
            mark = "✗" if f.severity is Severity.ERROR else "!"
            lines.append(f"  {mark} {f.path}  [{f.rule_id}/{f.severity.value}]")
            lines.append(f"      {f.message}")

    if ev.plan.steps:
        lines.append("")
        lines.append("迁移步骤建议（按执行顺序）：")
        for s in ev.plan.steps:
            rb = "可安全回滚" if s.rollback_safe else "不可安全回滚"
            lines.append(
                f"  {s.order:>2}. [{s.actor}/{s.severity}] {s.action}"
            )
            lines.append(f"       路径: {', '.join(s.paths)}（规则 {s.rule_id}，{rb}）")
            lines.append(f"       {s.detail}")
            lines.append(f"       回滚: {s.rollback}")

    lines.append("")
    lines.append("结论: " + ("放行 ✅" if ev.passed else "阻断 ❌"))
    return "\n".join(lines)


def _render_governance_human(ev: GovernanceEvaluation) -> str:
    r = ev.report
    lines: list[str] = []
    lines.append(
        f"数据契约发版评估：{r.old_version or '(无版本号)'} → {r.new_version or '(无版本号)'}"
    )

    # 上游自身兼容性（上下文）
    p = r.producer
    fwd = "✓" if p.forward_compatible else "✗"
    bwd = "✓" if p.backward_compatible else "✗"
    lines.append(
        f"上游自身兼容性：前向 {fwd}（{len(p.errors(Direction.FORWARD))} 个阻断项）"
        f" / 后向 {bwd}（{len(p.errors(Direction.BACKWARD))} 个阻断项）"
    )

    # 按下游的结论
    if not r.consumers:
        lines.append("下游评估：未提供下游画像（--consumer/--consumers），仅评估上游自身兼容性")
    else:
        critical_count = sum(1 for a in r.consumers if a.critical)
        lines.append(f"下游评估（共 {len(r.consumers)} 个，其中关键下游 {critical_count} 个）：")
    for a in r.consumers:
        tag = "关键" if a.critical else "非关键"
        errors, warns = a.errors, a.warnings
        if a.compatible:
            lines.append(f"  ✓ {a.name}（{tag}）：通过（{len(warns)} 个警告）")
        else:
            lines.append(f"  ✗ {a.name}（{tag}）：{len(errors)} 个阻断项, {len(warns)} 个警告")
        for f in a.findings:
            mark = "✗" if f.severity is Severity.ERROR else "!"
            lines.append(f"      {mark} {f.path}  [{f.rule_id}/{f.severity.value}]")
            lines.append(f"          {f.message}")

    # 迁移步骤（按执行者落到上游/具体下游）
    if r.plan.steps:
        lines.append("")
        lines.append("迁移步骤建议（按执行顺序；producer=上游，consumer:<名>=具体下游）：")
        for s in r.plan.steps:
            rb = "可安全回滚" if s.rollback_safe else "不可安全回滚"
            lines.append(f"  {s.order:>2}. [{s.actor}/{s.severity}] {s.action}")
            lines.append(f"       路径: {', '.join(s.paths)}（规则 {s.rule_id}，{rb}）")
            lines.append(f"       {s.detail}")
            lines.append(f"       回滚: {s.rollback}")

    # 治理放行口径
    lines.append("")
    if r.verdict is GateVerdict.PASS:
        verdict_label = "放行 ✅"
    elif r.verdict is GateVerdict.BLOCKED:
        verdict_label = "阻断 ❌"
    elif r.consumers:
        verdict_label = "放行（仅非关键下游受影响）⚠️"
    else:
        verdict_label = "放行（未提供下游画像，影响面未知）⚠️"
    lines.append(f"治理结论: {verdict_label}")
    for reason in r.blocking_reasons:
        lines.append(f"  ✗ {reason}")
    for reason in r.warning_reasons:
        lines.append(f"  ! {reason}")
    if not ev.passed and r.verdict is not GateVerdict.BLOCKED:
        lines.append(f"  （--fail-on {ev.fail_on} 阈值下判定为不通过）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datacontract",
        description="数据契约版本兼容性检查（CI 门禁）",
    )
    parser.add_argument("--version", action="version", version=f"datacontract {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="比对两个契约版本并判定兼容性")
    check.add_argument("old", help="旧版本契约 JSON 文件（消费者当前依赖的版本）")
    check.add_argument("new", help="新版本契约 JSON 文件（生产者即将发布的版本）")
    check.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="输出格式，默认 human",
    )
    check.add_argument(
        "--fail-on",
        choices=(FAIL_ERROR, FAIL_WARNING, FAIL_NEVER),
        default=FAIL_ERROR,
        help="阻断阈值：error=存在不兼容即阻断（默认）；warning=有警告也阻断；never=总放行",
    )

    evaluate_cmd = sub.add_parser(
        "evaluate",
        help="多下游发版评估：把上游变更投影到每个下游实际消费的数据结构上",
    )
    evaluate_cmd.add_argument("old", help="旧版本契约 JSON 文件（下游当前依赖的版本）")
    evaluate_cmd.add_argument("new", help="新版本契约 JSON 文件（上游即将发布的版本）")
    evaluate_cmd.add_argument(
        "--consumer",
        action="append",
        default=[],
        metavar="FILE",
        help="下游消费画像 JSON 文件，可重复指定多个",
    )
    evaluate_cmd.add_argument(
        "--consumers",
        action="append",
        default=[],
        metavar="DIR",
        help="下游画像目录（加载其中全部 *.json），可重复指定多个",
    )
    evaluate_cmd.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="输出格式，默认 human",
    )
    evaluate_cmd.add_argument(
        "--fail-on",
        choices=(FAIL_CRITICAL, FAIL_ANY, FAIL_NEVER),
        default=FAIL_CRITICAL,
        help=(
            "放行口径：critical=关键下游受损才阻断（默认）；"
            "any=任何下游受损都阻断；never=只出报告不阻断"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        try:
            ev = evaluate_paths(args.old, args.new, fail_on=args.fail_on)
        except ContractError as exc:
            print(f"契约非法: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except (OSError, json.JSONDecodeError) as exc:
            print(f"无法读取契约文件: {exc}", file=sys.stderr)
            return EXIT_USAGE

        if args.format == "json":
            print(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(_render_human(ev))
        return ev.exit_code()

    if args.command == "evaluate":
        consumer_files: list[str] = list(args.consumer)
        for directory in args.consumers:
            consumer_files.extend(
                str(p) for p in sorted(Path(directory).glob("*.json"))
            )
        try:
            ev = evaluate_governance_paths(
                args.old, args.new, consumer_files, fail_on=args.fail_on
            )
        except (ContractError, ConsumerError) as exc:
            print(f"输入非法: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except (OSError, json.JSONDecodeError) as exc:
            print(f"无法读取输入文件: {exc}", file=sys.stderr)
            return EXIT_USAGE

        if args.format == "json":
            print(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(_render_governance_human(ev))
        return ev.exit_code()

    parser.error(f"未知命令: {args.command}")  # pragma: no cover
    return EXIT_USAGE  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
