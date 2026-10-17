"""SeaTunnel HOCON 的确定性渲染与解析（be-10 §6.3 / §6.4）。

渲染与解析是一对反函数，共同构成本模块的对外面：

- :func:`render_hocon`：``EtlPlanModel + RenderContext → RenderedHocon``（同 Plan 必得同字节）
- :func:`render_sql`：Plan 的映射/变换/过滤 → 内层 SELECT
- :func:`parse_and_validate`：HOCON 文本 → ``HoconParseResult``（**从不抛异常**）

类型契约在 ``types.py``（渲染器、解析器、门禁三方共用，互不 import），
连接器模板在 ``connectors/``。两边都不导入 ``services/`` 或 ``api/``。
"""

from engines.hocon.parser import parse_and_validate
from engines.hocon.render import render_hocon, render_sql
from engines.hocon.types import (
    PARSER_VERSION,
    RENDERER_VERSION,
    SECRET_FIELDS,
    HoconParseError,
    HoconParseResult,
    RenderContext,
    RenderedHocon,
    SecretToken,
    assert_no_unresolved_tokens,
    find_tokens,
    parse_tokens,
    secret_token,
    secret_token_problems,
)

__all__ = [
    "PARSER_VERSION",
    "RENDERER_VERSION",
    "SECRET_FIELDS",
    "HoconParseError",
    "HoconParseResult",
    "RenderContext",
    "RenderedHocon",
    "SecretToken",
    "assert_no_unresolved_tokens",
    "find_tokens",
    "parse_and_validate",
    "parse_tokens",
    "secret_token_problems",
    "render_hocon",
    "render_sql",
    "secret_token",
]
