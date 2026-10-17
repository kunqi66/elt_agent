"""``check_syntax``：HOCON 可解析性 + 密钥渲染纪律（be-11 §5.1 第 2 步 / §6.1）。

**不重新解析**：解析结果由 service 传在 ``ctx.parse_result`` 里（be-11 §3.2）。
这里再调一次 parser 会造出「第二个解析器」——第一个改了它不会跟着改，
而且门禁就变回有 IO/第三方依赖的函数了。

两块内容，优先级从高到低：

1. **密钥渲染纪律（安全红线，最先报）**：HOCON 会落库、进账本、回给前端、参与指纹
   （hocon/types.py 的模块 docstring），所以连接字段的值**只允许**是
   ``{{secret:<id>:<field>}}`` 令牌或空。值既不是令牌也不是空 → block。
   文案里**绝不回显那个值**——只说「第 N 行的 password 字段不是令牌形态」，
   因为回显就等于把疑似口令又抄了一遍到日志/账本/前端。
2. **解析错误**：``parse_result.ok=False`` 时逐条转成 block finding。

## 新增/借用的 code

- ``syntax``（逐字取自 be-11 §6.1 的 parser issue code 表）：``HoconParseError``
  只有 ``message``/``line``/``column`` 三个字段，**没有** code，
  所以解析错误统一用最粗的那一级 ``syntax``；等 parser 补上细码（``missing_block`` /
  ``missing_key`` / ``unknown_plugin`` / ``bad_placeholder`` / ``bad_value``）后原样透传即可。
- ``bad_placeholder``（逐字取自 be-11 §6.1）：值以 ``{{secret:`` 开头但不是合法令牌，
  或令牌引用了白名单（``SECRET_FIELDS``）以外的字段名。
- ``plaintext_secret``（**新增**，be-11 §6.1 未定义）：值不是令牌形态、也看不出想写令牌——
  即疑似明文凭据。不复用 ``bad_value``：那个码会把「类型写错」与「明文口令出域」混成一类，
  而这两者的处置完全不同（前者是配置问题，后者是安全事件，必须能被单独统计）。
"""

from __future__ import annotations

from engines.gate.checks.base import (
    finalize_findings,
    iter_secret_tokens,
    safe_message,
    scan_plaintext_secret_fields,
)
from engines.gate.context import GateContext
from engines.gate.types import GateFinding, HoconLocator, make_finding
from engines.hocon.types import SECRET_FIELDS


def check_syntax(ctx: GateContext) -> list[GateFinding]:
    """HOCON 语法与渲染纪律。纯函数：只看 ``ctx``，不取时间、不做 IO。"""
    findings: list[GateFinding] = []
    hocon = ctx.hocon_content

    # 1) 安全红线：连接字段的值必须是令牌或空。**文案不回显值**。
    for lineno, key, kind in scan_plaintext_secret_fields(hocon):
        if kind == "bad_placeholder":
            message = (
                f"HOCON 第 {lineno} 行的 {key} 字段的密钥令牌格式不合法"
                "（应为 {{secret:<data_source_id>:<field>}}）。"
            )
            suggestion = "改成渲染器产出的令牌形态，例如 {{secret:1:password}}。"
        else:
            message = (
                f"HOCON 第 {lineno} 行的 {key} 字段不是密钥令牌形态，疑似明文凭据；"
                "制品会落库、进证据账本并回给前端，明文口令禁止出现在这里。"
            )
            suggestion = "把该值替换为密钥令牌 {{secret:<data_source_id>:<field>}}，真值只经 Vault 物化。"
        findings.append(
            make_finding(
                check_id="syntax",
                code=kind,
                severity="block",
                message=safe_message(message),
                locator=HoconLocator(line=lineno, column=1),
                suggestion=safe_message(suggestion),
            )
        )

    # 2) 令牌本身合法，但引用了白名单以外的字段 → 执行器不会替换它，等于把字面量当口令发出去。
    for lineno, _ds_id, field in iter_secret_tokens(hocon):
        if field in SECRET_FIELDS:
            continue
        findings.append(
            make_finding(
                check_id="syntax",
                code="bad_placeholder",
                severity="block",
                message=safe_message(
                    f"HOCON 第 {lineno} 行的令牌引用了不允许令牌化的字段 {field}；"
                    f"白名单为 {list(SECRET_FIELDS)}。"
                ),
                locator=HoconLocator(line=lineno, column=1),
                suggestion="改用白名单内的字段名，或把该值移出连接配置。",
            )
        )

    # 3) 解析错误。放在最后：明文凭据是红线，先报它，免得被几十条语法错淹掉。
    if not ctx.parse_result.ok:
        for error in ctx.parse_result.errors:
            findings.append(
                make_finding(
                    check_id="syntax",
                    code="syntax",
                    severity="block",
                    message=safe_message(
                        f"HOCON 第 {error.line} 行第 {error.column} 列无法解析：{error.message}"
                    ),
                    locator=HoconLocator(line=error.line, column=error.column),
                    suggestion="修正该处的 HOCON 语法后重跑门禁。",
                )
            )

    return finalize_findings("syntax", findings)
