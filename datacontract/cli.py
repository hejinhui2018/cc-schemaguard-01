"""CI 集成入口。

两种用法：

1. 命令行::

       python -m datacontract check old.json new.json
       python -m datacontract check old.json new.json --format json --fail-on error

   退出码：
       0  放行（按 --fail-on 阈值没有命中）
       1  阻断（存在不兼容；--fail-on warning 时警告也阻断）
       2  用法错误或契约本身非法

2. 可编程::

       from datacontract import evaluate_paths
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
from .diff import diff_contracts
from .migration import MigrationPlan, plan_migration
from .parser import Contract, ContractError, parse_file

__version__ = "0.1.0"

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

    parser.error(f"未知命令: {args.command}")  # pragma: no cover
    return EXIT_USAGE  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
