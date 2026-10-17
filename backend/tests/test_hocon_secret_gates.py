"""两道密钥令牌闸判的是两件不同的事，不能互相代替。

`hocon_artifact.content` 会落库、进账本、返回前端、参与指纹，所以渲染器**永远不写口令**，
只写 ``{{secret:<data_source_id>:<字段>}}``，由 worker 在执行前物化。围绕它有两道闸：

- **执行前**（:func:`assert_no_unresolved_tokens`）：令牌必须**一个不剩**（忘了替换 =
  把字面量 ``{{secret:1:password}}`` 当口令发出去）；
- **冻结前**（:func:`secret_token_problems`）：令牌必须**解得开**（数据源还在、还有凭据），
  否则这份制品审批通过也执行不了。

**这条用例的来历**：`freeze_version` 一开始用错了闸——拿「执行前」那道去卡冻结，
于是**每一份合法制品都冻不了**（制品本来就该带令牌），报错还写着
「仍有未替换的密钥令牌」，读起来像渲染器漏了替换。两道闸的语义差别必须有用例钉住。
"""

from __future__ import annotations

import pytest

from engines.hocon import (
    SECRET_FIELDS,
    assert_no_unresolved_tokens,
    find_tokens,
    parse_tokens,
    secret_token,
    secret_token_problems,
)

_HOCON = """
source {
  Jdbc {
    url = "{{secret:1:jdbc_url}}"
    user = "{{secret:1:username}}"
    password = "{{secret:1:password}}"
  }
}
"""


def test_执行前的闸只认令牌在不在() -> None:
    with pytest.raises(ValueError, match="仍有未替换的密钥令牌"):
        assert_no_unresolved_tokens(_HOCON, where="execution#1")
    # 替换干净了就放行（三个令牌都得换掉——只换一个仍然会被拒，这正是这道闸的用途）
    resolved = _HOCON.replace("{{secret:1:jdbc_url}}", "jdbc:mysql://h/db").replace(
        "{{secret:1:username}}", "elt_rw"
    ).replace("{{secret:1:password}}", "***")
    assert_no_unresolved_tokens(resolved, where="execution#1")


def test_带令牌的制品可以被冻结检查放行() -> None:
    """**关键差别**：执行前的闸会拦下这份制品，冻结前的闸必须放行它。"""
    with pytest.raises(ValueError):
        assert_no_unresolved_tokens(_HOCON, where="freeze")

    problems = secret_token_problems(_HOCON, resolvable_data_source_ids=frozenset({1}))
    assert problems == (), f"令牌指向的数据源有凭据，不该报问题：{problems}"


def test_数据源没有可用凭据时报出来() -> None:
    problems = secret_token_problems(_HOCON, resolvable_data_source_ids=frozenset())
    assert len(problems) == 3
    assert all("没有可用凭据" in p for p in problems)


def test_字段不在白名单时报出来() -> None:
    # 手工改过的制品 / 渲染器写错字：字段名不在白名单，执行期不会有人去替换它
    problems = secret_token_problems(
        'password = "{{secret:1:passwd}}"', resolvable_data_source_ids=frozenset({1})
    )
    assert len(problems) == 1
    assert "白名单" in problems[0]


def test_令牌构造器拦住白名单外的字段() -> None:
    for field in SECRET_FIELDS:
        assert secret_token(3, field) == f"{{{{secret:3:{field}}}}}"
    with pytest.raises(ValueError, match="不允许令牌化"):
        secret_token(3, "passwd")


def test_解析出的令牌与字面串两份视图一致() -> None:
    """``find_tokens``（给人看）与 ``parse_tokens``（给查库用）必须同源，
    否则「报给用户的令牌」与「实际去查的数据源」会不是同一个。"""
    literals = find_tokens(_HOCON)
    parsed = parse_tokens(_HOCON)
    assert len(literals) == len(parsed) == 3
    for literal, token in zip(literals, parsed, strict=True):
        assert literal == f"{{{{secret:{token.data_source_id}:{token.field}}}}}"
