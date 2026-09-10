"""数据契约治理库（仅标准库）。

公共 API：

- parse_contract / parse_dict / parse_text / parse_file：契约解析与规范化
- diff_contracts：结构化版本差异
- check_compatibility：兼容性判定（forward / backward / full）
- plan_migration：有序迁移步骤
- CompatibilityReport：CI 可编程判定结果
"""

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
from .parser import ContractError, parse_contract, parse_dict, parse_file, parse_text
from .diff import Change, ChangeKind, diff_contracts
from .compatibility import (
    CompatibilityReport,
    Direction,
    Finding,
    Severity,
    check_compatibility,
)
from .migration import MigrationStep, MigrationPlan, plan_migration
from .cli import (
    Evaluation,
    EXIT_INCOMPATIBLE,
    EXIT_OK,
    EXIT_USAGE,
    evaluate,
    evaluate_paths,
)

__all__ = [
    "ArrayType",
    "Contract",
    "Field",
    "ObjectType",
    "RefType",
    "ScalarType",
    "TypeNode",
    "describe_type",
    "ContractError",
    "parse_contract",
    "parse_dict",
    "parse_file",
    "parse_text",
    "Change",
    "ChangeKind",
    "diff_contracts",
    "CompatibilityReport",
    "Direction",
    "Finding",
    "Severity",
    "check_compatibility",
    "MigrationStep",
    "MigrationPlan",
    "plan_migration",
    "Evaluation",
    "evaluate",
    "evaluate_paths",
    "EXIT_OK",
    "EXIT_INCOMPATIBLE",
    "EXIT_USAGE",
]

__version__ = "0.1.0"
