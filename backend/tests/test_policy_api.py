"""be-07 预演接口的**接线**测试。

不落 HTTP 请求：那需要连 PG（本机没有 Docker），而这里要挡的是
另一类事故——**路由没被挂上**（`api/router.py` 漏 include、前缀写错）。
症状是 404，前端会当成「后端还没做」，而后端以为已经做了。

所以断言走 `app.openapi()["paths"]`。**不能数 `app.routes`**：
新版 Starlette 的 ``include_router`` 把子路由包成 ``_IncludedRouter``，
按路径遍历看不到它们，会得出「路由不存在」的错误结论。
"""

from __future__ import annotations

from main import create_app

PREVIEW_PATH = "/api/v1/governance/policies/preview"


def test_预演路由已挂载() -> None:
    paths = create_app().openapi()["paths"]
    assert PREVIEW_PATH in paths
    assert "post" in paths[PREVIEW_PATH]


def test_预演请求体不含租户字段() -> None:
    """线上形态里根本没有 tenant_id：租户只能来自登录态。

    这条断言是防回归的——把 `PolicyPreviewScope` 换成引擎的 `ResourceScope`
    （它把 tenant_id 定为必填）会让请求体多出这个字段，
    而「多一个可选字段」在 code review 里几乎不会被注意到。
    """
    schema = create_app().openapi()
    scope_ref = schema["components"]["schemas"]["PolicyPreviewScope"]["properties"]
    assert "tenant_id" not in scope_ref
    assert "project_id" not in scope_ref
    # 资源引用的字段名与引擎 DTO 一致（含别名为 schema 的那一个）
    ref = schema["components"]["schemas"]["ResourceRef"]["properties"]
    assert {"data_source_type", "database", "table", "env"} <= set(ref)


def test_响应体复用引擎DTO并带出版本号() -> None:
    """响应里必须有 `policy_version`：同一份输入在不同策略版本下等级不同，
    没有这个字段，两次调用结果的差异就无从解释。"""
    schema = create_app().openapi()
    decision = schema["components"]["schemas"]["PolicyDecision"]["properties"]
    for field in ("level", "blocked", "required_slots", "hits", "budget", "policy_version"):
        assert field in decision
