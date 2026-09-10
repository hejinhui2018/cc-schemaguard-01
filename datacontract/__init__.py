"""数据契约治理库（仅标准库）。

公共 API：

- parse_contract / parse_dict / parse_text / parse_file：契约解析与规范化
- diff_contracts：结构化版本差异
- check_compatibility：兼容性判定（forward / backward / full）
- plan_migration：有序迁移步骤
- CompatibilityReport：CI 可编程判定结果
- ConsumerProfile / parse_consumer_file：下游消费画像
- evaluate_governance：多下游发版评估（治理放行口径）
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
from .consumer import (
    ConsumerError,
    ConsumerProfile,
    parse_consumer_dict,
    parse_consumer_file,
)
from .governance import (
    FAIL_ANY,
    FAIL_CRITICAL,
    FAIL_NEVER,
    ConsumerAssessment,
    GateVerdict,
    GovernanceEvaluation,
    GovernancePlan,
    GovernanceReport,
    evaluate_governance,
)
from .cli import (
    Evaluation,
    EXIT_INCOMPATIBLE,
    EXIT_OK,
    EXIT_USAGE,
    evaluate,
    evaluate_governance_paths,
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
    "ConsumerError",
    "ConsumerProfile",
    "parse_consumer_dict",
    "parse_consumer_file",
    "ConsumerAssessment",
    "GateVerdict",
    "GovernanceEvaluation",
    "GovernancePlan",
    "GovernanceReport",
    "evaluate_governance",
    "evaluate_governance_paths",
    "FAIL_CRITICAL",
    "FAIL_ANY",
    "FAIL_NEVER",
    "Evaluation",
    "evaluate",
    "evaluate_paths",
    "EXIT_OK",
    "EXIT_INCOMPATIBLE",
    "EXIT_USAGE",
]

__version__ = "0.2.0"
