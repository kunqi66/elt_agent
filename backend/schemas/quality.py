"""质量报告与错误分流的 HTTP DTO（be-17 §3.1）。

本模块只做两件事：

1. **再导出** ``engines/quality/`` 的领域模型（契约、编译产物、报告），让 ``api/`` /
   ``services/`` 不必从 ``engines`` 里挑路径——跨层只传 Pydantic 模型（CLAUDE.md §4.6）；
2. 补上**只有 HTTP 才有的形状**：游标分页、错误码汇总、错误行副本。

``extra="ignore"`` 用于**读**（库里存的 jsonb 可能比 DTO 多键，多一个键不该让只读接口
500）；写侧的 ``QualityContractSpec`` / ``CompiledRule`` 用 ``extra="forbid"``
（少一个必填键 = 静默丢字段，那才是危险的）。

时间不做归一化：``as_utc`` 是**存储边界**的翻译（见 ``core/ids.py`` 的 docstring），
按约定归服务层；本模块只声明类型，不偷偷改语义。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from engines.quality import (
    ContractError,
    ErrorSample,
    QualityContractSpec,
    QualityDiagnosticItem,
    QualityDiagnostics,
    QualityReportDTO,
    QualityRuleSpec,
)

#: 读模型的统一配置（未知键容忍）。
_READ = ConfigDict(extra="ignore")

__all__ = [
    "ContractError",
    "CursorPage",
    "ErrorCodeSummaryDTO",
    "ErrorRecordDTO",
    "ErrorSample",
    "FieldHitDTO",
    "QualityContractSpec",
    "QualityDiagnosticItem",
    "QualityDiagnostics",
    "QualityReportDTO",
    "QualityRuleSpec",
]


class CursorPage[T](BaseModel):
    """游标分页。``next_cursor`` 为 None 表示到底了。

    没有 ``total``：错误行可能是百万级，给总数的代价是一次全表 count，
    而它对「翻下一页」这件事毫无帮助。与 ``schemas/governance.py::EvidencePage`` 同一取舍。
    """

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None


class ErrorRecordDTO(BaseModel):
    """错误行的**控制面副本**：只有摘要，没有原文（CLAUDE.md §5.7）。

    ``payload_digest`` 是「这行长什么样」在控制面唯一的线索，不可逆，也**不能当可逆
    信息用**（低熵字段的组合可被字典攻击，be-17 §6.3 明确禁止把它投喂 LLM 或做
    「还原看看到底错在哪」）。``reason`` 是规则模板 + 字段名，由数据面生成，同样不含值。
    """

    model_config = _READ

    id: int
    run_id: int
    rule_id: str
    error_code: str
    field_name: str
    payload_digest: str
    reason: str
    occurred_at: datetime


class FieldHitDTO(BaseModel):
    """违规字段的命中计数（前端图表用）。"""

    model_config = _READ

    field_name: str
    count: int


class ErrorCodeSummaryDTO(BaseModel):
    """``GET …/errors/summary``：错误码分布 + 规则命中 + 违规字段 Top-N（be-17 §3.1）。

    数据来源是**已经落库的报告**（``quality_report``）加上控制面错误副本的分组统计，
    因此这个接口不碰目标端、不依赖作业还在跑——图表面板在报告生成后永远可读。
    报告不存在时抛 ``NOT_FOUND``：没有报告就没有 ``passed``，用 0 冒充「通过」更危险。
    """

    model_config = _READ

    run_id: int
    report_id: int
    #: 能否发布。与 ``quality-report`` 接口同源（同一行数据），不是一个独立判断。
    passed: bool = False
    blocked_reason: str | None = None
    failed_rules: list[str] = Field(default_factory=list)
    valid_rows: int = 0
    rejected_rows: int = 0
    processed_rows: int = 0
    read_rows: int = 0
    reject_rate: float = 0.0
    reject_rate_budget: float = 0.0
    budget_used_ratio: float | None = None
    #: ``error_code -> 行数``（全量，不只是 Top-N）。
    error_code_dist: dict[str, int] = Field(default_factory=dict)
    #: ``rule_id -> 命中行数``（含 ``warn`` 规则——它在错误表里没有痕迹，是在影子表上数的）。
    rule_hits: dict[str, int] = Field(default_factory=dict)
    top_fields: list[FieldHitDTO] = Field(default_factory=list)
    #: 控制面副本的行数与被截断标志。副本有上限（错误表可能百万行），
    #: ``truncated=True`` 时前端要说清「明细不全，完整明细在目标端」。
    error_records_copied: int = 0
    error_records_truncated: bool = False
    interrupted: bool = False
    contract_sha256: str = ""
    generated_at: datetime | None = None
