"""领域枚举。

一律用 ``StrEnum`` 并以 ``varchar + CHECK`` 落库（**不用** PG enum 类型）：
新增取值只需改代码 + 一条 migration，不必 ``ALTER TYPE``（后者在事务里很别扭）。

取值即对外契约：前端与 Benchmark 期望值都按这些字符串匹配，改动等同于破坏兼容。
"""

from __future__ import annotations

from enum import StrEnum


class Env(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"  # PDP 对 prod 环境的判定显著不同


class UserStatus(StrEnum):
    ACTIVE = "active"
    LOCKED = "locked"
    DISABLED = "disabled"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MemberStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


class RoleName(StrEnum):
    """职责槽。四眼与自批拦截都基于此判定。"""

    MAKER = "maker"
    CHECKER_DATA = "checker_data"
    CHECKER_SECURITY = "checker_security"
    OPERATOR = "operator"
    AUDITOR = "auditor"

    @classmethod
    def checker_slots(cls) -> tuple[RoleName, RoleName]:
        return (cls.CHECKER_DATA, cls.CHECKER_SECURITY)


class DataSourceType(StrEnum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    DORIS = "doris"
    ORACLE = "oracle"  # 仅注册接口，不真连
    CLICKHOUSE = "clickhouse"  # 仅注册接口，不真连
    CSV = "csv"
    EXCEL = "excel"  # 仅注册接口，不真连
    JSON = "json"
    PARQUET = "parquet"
    MINIO = "minio"
    REST = "rest"  # 仅注册接口，不真连


class SensitiveLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ProbeStatus(StrEnum):
    PROBING = "probing"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class PipelineStatus(StrEnum):
    """PRD §8.2 的 16 个状态。"""

    DRAFT = "draft"
    GENERATED = "generated"
    GATE_FAILED = "gate_failed"
    VERSION_FROZEN = "version_frozen"
    PREPARING = "preparing"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    COMMITTABLE = "committable"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class RiskLevel(StrEnum):
    """PDP 输出的风险等级。判定矩阵见《需求对齐记录》§二.3。

    **比较语义是「严重程度」，越大越危险**：``P0 > P1 > P2 > P3``。
    这样 ``max()`` 给出最严的一档，且 ``level >= RiskLevel.P1`` 读作「至少和 P1 一样严」。
    ``rank`` 保留「越小越危险」的编号直觉（与 P0..P3 命名一致），比较运算符则反向映射到它。
    StrEnum 默认按字符串比较——``"P0" < "P1"`` 只是字面巧合，一旦引入 P10 之类取值
    就会静默反过来，且 ``max()`` 会返回最**不**危险的等级。故此处必须显式定义。
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @property
    def rank(self) -> int:
        return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[self.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank < other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank <= other.rank

    @classmethod
    def most_severe(cls, *levels: RiskLevel) -> RiskLevel:
        """取最严的一档。空入参是调用方 bug，不返回默认值掩盖。"""
        if not levels:
            raise ValueError("most_severe() 至少需要一个风险等级")
        return max(levels)

    @property
    def must_block(self) -> bool:
        """P0 直接阻断，不进入审批与执行。"""
        return self is RiskLevel.P0

    @property
    def requires_hardening(self) -> bool:
        """P1 不阻断，但必须双审批 + 加固后才能提交。"""
        return self in (RiskLevel.P0, RiskLevel.P1)

    @property
    def min_approval_slots(self) -> int:
        """提交前必须集齐的审批槽数量。

        P3 是只读探查，免审批；P0 已被阻断，槽位数无意义故为 0；
        P1/P2 都需要数据槽 + 安全槽各一。
        """
        return {RiskLevel.P0: 0, RiskLevel.P1: 2, RiskLevel.P2: 2, RiskLevel.P3: 0}[self]


class PreparationStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_APPROVED = "partially_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"  # 指纹变化后由新准备单取代


class ApprovalSlot(StrEnum):
    CHECKER_DATA = "checker_data"
    CHECKER_SECURITY = "checker_security"


class ApprovalDecisionType(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    RETURN = "return"  # 退回补充


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # 监督超限硬中断
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"

    @classmethod
    def active_states(cls) -> tuple[RunStatus, ...]:
        """活跃态：同一 PipelineVersion 任一时刻只允许一条。"""
        return (cls.QUEUED, cls.RUNNING, cls.PAUSED, cls.INTERRUPTED, cls.ROLLING_BACK)


class ToolName(StrEnum):
    """副作用唯一出口的工具注册表（见《概要设计总纲》§5）。"""

    SEATUNNEL_SUBMIT = "seatunnel.submit"
    SEATUNNEL_STOP = "seatunnel.stop"
    TARGET_ENSURE_TABLES = "target.ensure_tables"
    TARGET_PUBLISH_SWAP = "target.publish_swap"
    TARGET_ROLLBACK_CLEANUP = "target.rollback_cleanup"
    DATASOURCE_CONNECTIVITY_TEST = "datasource.connectivity_test"
    METADATA_PROBE = "metadata.probe"


class ErrorCodeLabel(StrEnum):
    """质量契约的错误码标签，与质量报告、错误表分流口径一致。"""

    E_TYPE_CAST = "E_TYPE_CAST"
    E_NULL_PK = "E_NULL_PK"
    E_REGEX_MISMATCH = "E_REGEX_MISMATCH"
    E_RANGE_VIOLATION = "E_RANGE_VIOLATION"
    E_ENUM_INVALID = "E_ENUM_INVALID"
    E_DUPLICATE_KEY = "E_DUPLICATE_KEY"
    E_SENSITIVE_PLAINTEXT = "E_SENSITIVE_PLAINTEXT"
    E_SCHEMA_DRIFT = "E_SCHEMA_DRIFT"


class BenchmarkLayer(StrEnum):
    L0 = "L0"  # 静态注入
    L1 = "L1"  # 模拟故障
    L2 = "L2"  # 真实链路


class BenchmarkRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"  # 超过最大重试次数，需人工介入


class EvidenceEventType(StrEnum):
    """关键事件类型。缺失的类型直接在枚举里加，不要在代码里裸写字符串。"""

    AUTH_LOGIN_SUCCEEDED = "auth.login_succeeded"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGGED_OUT = "auth.logged_out"
    AUTH_LOGOUT_ALL = "auth.logout_all"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    MEMBER_ADDED = "member.added"
    MEMBER_STATUS_CHANGED = "member.status_changed"
    MEMBER_ROLE_GRANTED = "member.role_granted"
    MEMBER_ROLE_REVOKED = "member.role_revoked"
    DUTY_CONFLICT_DENIED = "member.duty_conflict_denied"
    TENANT_MISMATCH_DENIED = "auth.tenant_mismatch_denied"
    DATASOURCE_CREATED = "datasource.created"
    DATASOURCE_CONNECTIVITY_TESTED = "datasource.connectivity_tested"
    METADATA_PROBED = "metadata.probed"
    SECRET_MATERIALIZED = "secret.materialized"
    PIPELINE_GENERATED = "pipeline.generated"
    PIPELINE_GATE_EVALUATED = "pipeline.gate_evaluated"
    PIPELINE_VERSION_FROZEN = "pipeline.version_frozen"
    PREPARATION_CREATED = "preparation.created"
    # 过期与被取代也入账：前端的时间线要把「什么时候失效的」画出来，
    # 而这两个事实**只由 status 的一次 UPDATE 表达**——不落账本的话，
    # 「这份准备单是被新单取代的，还是过期没管的」事后无法回答，
    # 因为 status 的当前值只说明结局、不说明时刻与原因。
    PREPARATION_EXPIRED = "preparation.expired"
    PREPARATION_SUPERSEDED = "preparation.superseded"
    POLICY_EVALUATED = "policy.evaluated"
    POLICY_BLOCKED = "policy.blocked"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_RETURNED = "approval.returned"
    APPROVAL_SELF_DENIED = "approval.self_denied"
    # 审批时发现指纹已变（制品/输入被改过）。它和「自批」一样是**协议类拒绝**：
    # 有人拿着一份「当时批过、现在已经不是那个东西」的制品来推进流程，
    # 这件事必须留痕——不记的话，一次被拦下的过期审批与「没人动过」在审计上无法区分。
    APPROVAL_STALE_DENIED = "approval.stale_denied"
    # 审批时的职责混用（maker 兼任 checker，或两个 checker 槽同一人）。
    # 与 `member.duty_conflict_denied` 分开记：那条是「授予角色时被拒」，
    # 这条是「审批时被拒」——两处都拦是因为跨行约束 CHECK 表达不了，
    # 而合成一个码会让「谁在什么时候试图兼任」这个问题查不出来。
    APPROVAL_DUTY_DENIED = "approval.duty_denied"
    CAPABILITY_ISSUED = "capability.issued"
    CAPABILITY_REJECTED = "capability.rejected"
    CAPABILITY_CONSUMED = "capability.consumed"
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RUN_INTERRUPTED = "run.interrupted"
    SUPERVISION_WARNING = "supervision.warning"
    QUALITY_REPORT_GENERATED = "quality.report_generated"
    SWAP_SUCCEEDED = "swap.succeeded"
    SWAP_FAILED = "swap.failed"
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_SUCCEEDED = "rollback.succeeded"
    BENCHMARK_RUN_COMPLETED = "benchmark.run_completed"
    LEDGER_CHECKPOINT_SIGNED = "ledger.checkpoint_signed"
    # 「有人试图读账本但被拒」。be-06 §7 第 6 行要求这条拒绝本身入账：
    # 审计日志被越权查询，恰恰是审计员最需要看到的事件
    LEDGER_ACCESS_DENIED = "ledger.access_denied"
