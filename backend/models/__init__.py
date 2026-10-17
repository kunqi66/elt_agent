"""全部 ORM 模型的唯一出口。

**Alembic autogenerate 与 ``create_all`` 都依赖「导入本包即导入全部模型」**，
因此新模型必须在此登记，否则迁移会静默漏表。
"""

from models.base import (
    Base,
    CreatedAtMixin,
    ImmutableViolationError,
    PKMixin,
    ProjectScopedMixin,
    TenantMixin,
    TimestampMixin,
)
from models.benchmark import BenchmarkCase, BenchmarkRun, BenchmarkSuite
from models.datasource import DataSource, MetadataProfile, SecretReference
from models.evidence import EvidenceEvent, EvidenceLedgerBlock, OutboxMessage
from models.execution import ErrorRecord, ExecutionMetric, ExecutionRun, QualityReport
from models.governance import ApprovalDecision, Capability, PolicyRule, Preparation, RiskPolicyVersion
from models.pipeline import (
    EtlPlan,
    HoconArtifact,
    Pipeline,
    PipelineConversation,
    PipelineVersion,
    QualityContract,
    RuntimeSupervisionContract,
)
from models.support import IdempotencyKey, LoginAttempt
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment, Tenant

__all__ = [
    # base
    "Base",
    "CreatedAtMixin",
    "ImmutableViolationError",
    "PKMixin",
    "ProjectScopedMixin",
    "TenantMixin",
    "TimestampMixin",
    # tenant
    "Tenant",
    "AppUser",
    "Project",
    "ProjectMember",
    "RoleAssignment",
    # datasource
    "SecretReference",
    "DataSource",
    "MetadataProfile",
    # pipeline
    "Pipeline",
    "PipelineConversation",
    "EtlPlan",
    "HoconArtifact",
    "QualityContract",
    "RuntimeSupervisionContract",
    "PipelineVersion",
    # governance
    "PolicyRule",
    "RiskPolicyVersion",
    "Preparation",
    "ApprovalDecision",
    "Capability",
    # execution
    "ExecutionRun",
    "ExecutionMetric",
    "QualityReport",
    "ErrorRecord",
    # evidence
    "EvidenceEvent",
    "EvidenceLedgerBlock",
    "OutboxMessage",
    # benchmark
    "BenchmarkSuite",
    "BenchmarkCase",
    "BenchmarkRun",
    # support
    "IdempotencyKey",
    "LoginAttempt",
]
