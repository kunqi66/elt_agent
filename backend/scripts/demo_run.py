"""演示链路一键跑通（PRD §14 的演示场景一 / 二）。

**为什么要有这个脚本**：整条链路有 9 个人工步骤（注册数据源 → 探查 → 生成 → 澄清
→ 冻结 → 准备单 → 两道审批 → 提交执行 → 等收尾），每一步都依赖上一步的 id，
而 PRD §14 的验收口径是「**可从空环境一键跑通**」。靠人照着文档敲 curl 的结果是
「文档里漏了一步，谁也不知道是哪一步」——脚本本身就是那份文档的可执行版本。

**它不是测试**：不 mock、不建临时库、不清理现场。它打的是真的 HTTP 接口、
真的 worker、真的 SeaTunnel，产物（版本 / 准备单 / 运行 / 账本）全部留在库里
供人用界面翻。`pytest` 覆盖的是单元与集成路径，这个脚本覆盖的是**部署路径**。

用法（先按 CLAUDE.md §7 起好隧道 / API / worker）::

    cd backend
    ELT_DEMO_PASSWORD='<开张时定的口令>' uv run python scripts/demo_run.py --scenario 1
    ELT_DEMO_PASSWORD='...' uv run python scripts/demo_run.py --scenario 2

环境变量：

* ``ELT_DEMO_PASSWORD``  必填。开张时（``scripts/bootstrap_tenant.py``）给五个演示用户设的同一个口令。
* ``ELT_DEMO_TENANT``    默认 ``demo2``（租户开张脚本建的那个）。
* ``ELT_API_BASE``       默认 ``http://127.0.0.1:8000``。
* ``ELT_DEMO_TARGET``    默认 ``doris``；``mysql`` 时改用 MySQL 目标端（Doris 起不来时的退路）。

口令只从环境变量进，不进命令行、不进文件（CLAUDE.md §5.2）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API_BASE = os.environ.get("ELT_API_BASE", "http://127.0.0.1:8000")
TENANT = os.environ.get("ELT_DEMO_TENANT", "demo2")
PROJECT_CODE = "sales_wide"

#: 五个人各有职责槽，缺一个就凑不出「四眼」
USERS = {
    "maker": "maker1",
    "checker_data": "checker.data1",
    "checker_security": "checker.security1",
    "operator": "operator1",
    "auditor": "auditor1",
}

#: 场景一的需求原文。**刻意把目标库表名写在句子里**（``dw_demo.orders_wide``）：
#: 兜底起草器按「需求里点名的目标表名优先」取（``engines/plan/draft.py::_pick_target_name``），
#: 写得含糊时会退化成 ``{源表}_wide``，于是演示写到的表与真正建的表对不上。
REQUIREMENTS = {
    1: (
        "把数据源 1 里 biz_demo.orders 的订单数据同步到 dw_demo.orders_wide，"
        "字段一一对应，按 order_id 增量抽取。"
    ),
    2: (
        "把数据源 1 里 biz_demo.orders 的订单数据同步到 dw_demo.orders_wide，"
        "金额必须大于 0，状态只能是 PAID/PENDING/REFUNDED/CANCELLED 之一，"
        "不合规的行分流到错误表，拒绝率超过阈值就中断。"
    ),
}


class DemoError(RuntimeError):
    """脚本自己判定的失败（区别于接口返回的业务错误）。"""


class Demo:
    def __init__(self, scenario: int, target: str, project_id: int, password: str) -> None:
        self.scenario = scenario
        self.target = target
        self.project_id = project_id
        self.password = password
        self.client = httpx.Client(base_url=API_BASE, timeout=60.0)
        self.tokens: dict[str, str] = {}
        self.user_ids: dict[str, int] = {}

    # ------------------------------------------------------------ 基础设施

    def login_all(self) -> None:
        for role, username in USERS.items():
            body = {"username": username, "password": self.password, "tenant_code": TENANT}
            data = self._call("POST", "/api/v1/auth/login", json=body)["data"]
            self.tokens[role] = data["access_token"]
            self.user_ids[role] = data["user"]["id"]
            print(f"    ✓ {username:<18} id={data['user']['id']}")

    def _headers(self, role: str, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.tokens[role]}"}
        if idempotency_key:
            # 所有可能重放的写接口都要求它（CLAUDE.md §6）。重跑脚本时同一步骤用同一个
            # key，接口就会把第二次调用当成重放（回放首次结果）而不是又建一份制品。
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        role: str = "maker",
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self.client.request(
            method, path, headers=self._headers(role, idempotency_key), **kwargs
        )
        try:
            payload = response.json()
        except ValueError:  # pragma: no cover - 非 JSON 响应说明网关/代理出了问题
            raise DemoError(f"{method} {path} → HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise DemoError(
                f"{method} {path} → HTTP {response.status_code} "
                f"{payload.get('code')}: {payload.get('message')} {payload.get('details') or ''}"
            )
        return payload

    # ------------------------------------------------------------ 数据源

    def ensure_datasource(self, *, name: str, ds_type: str, endpoint: dict[str, Any],
                          credentials: dict[str, str], probe: bool) -> int:
        existing = self._call(
            "GET", "/api/v1/datasources", params={"project_id": self.project_id}
        )["data"]
        items = existing.get("list") or []
        for item in items:
            if item["name"] == name:
                ds_id = int(item["id"])
                print(f"    · 复用数据源 {name} (id={ds_id})")
                break
        else:
            ds_id = int(
                self._call(
                    "POST",
                    "/api/v1/datasources",
                    idempotency_key=f"demo-ds-{name}",
                    json={
                        "project_id": self.project_id,
                        "name": name,
                        "type": ds_type,
                        "env": "dev",
                        "endpoint": endpoint,
                        "credentials": credentials,
                    },
                )["data"]["id"]
            )
            print(f"    ✓ 新建数据源 {name} (id={ds_id})")

        self._wait_connectivity(ds_id, name)
        if probe:
            self._probe(ds_id, name)
        return ds_id

    def _wait_connectivity(self, ds_id: int, name: str) -> None:
        """连通性测试是**异步**的（202 + test_id），由 worker 经 ToolBroker 执行。

        轮询而不是「发完就往下走」：测试没通过时后面的探查会以「连接失败」收场，
        而那个错看起来像凭据问题——真因却是测试还没跑完。
        """
        test_id = self._call(
            "POST", f"/api/v1/datasources/{ds_id}/connectivity-tests",
            idempotency_key=f"demo-conn-{ds_id}-{int(time.time())}", json={},
        )["data"]["test_id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            result = self._call(
                "GET", f"/api/v1/datasources/{ds_id}/connectivity-tests/{test_id}"
            )["data"]
            if result["state"] not in {"pending", "running"}:
                ok = result.get("ok")
                print(f"    · {name} 连通性：{result['state']} ok={ok} {result.get('detail') or ''}")
                if not ok:
                    raise DemoError(f"{name} 连通性测试未通过：{result}")
                return
            time.sleep(1)
        raise DemoError(f"{name} 连通性测试 60 秒未结束（worker 没消费？看 worker 日志）")

    def _probe(self, ds_id: int, name: str) -> None:
        """元数据探查：生成链路要靠它拿到源表结构与脱敏样本。"""
        profile = self._call(
            "POST", f"/api/v1/datasources/{ds_id}/probes",
            idempotency_key=f"demo-probe-{ds_id}-{int(time.time())}", json={},
        )["data"]
        if profile.get("status") == "ready":
            print(f"    · {name} 探查完成 v{profile.get('version')}")
            return
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            profiles = self._call("GET", f"/api/v1/datasources/{ds_id}/profiles")["data"]
            latest = (profiles.get("list") or profiles.get("items") or [])[:1]
            if latest and latest[0].get("status") == "ready":
                print(f"    · {name} 探查完成 v{latest[0].get('version')}")
                return
        raise DemoError(f"{name} 探查 120 秒未完成")

    # ------------------------------------------------------------ 生成链路

    def generate(self, source_id: int, target_id: int, requirement: str) -> int:
        snapshot = self._call(
            "POST", "/api/v1/pipelines/generations",
            idempotency_key=f"demo-gen-{self.scenario}-{int(time.time())}",
            json={
                "project_id": self.project_id,
                "requirement": requirement,
                "source_data_source_id": source_id,
                "target_data_source_id": target_id,
            },
        )["data"]
        pipeline_id = int(snapshot["thread_id"])
        rounds = 0
        while snapshot["phase"] == "awaiting_clarification":
            rounds += 1
            if rounds > 6:
                raise DemoError("澄清轮次超过 6 轮，疑似在空转")
            answers = self._answer(snapshot["pending_questions"])
            print(f"    · 第 {rounds} 轮澄清：{len(answers)} 个问题")
            for key, value in answers.items():
                print(f"        {key} = {value}")
            snapshot = self._call(
                "POST", f"/api/v1/pipelines/generations/{pipeline_id}/answers",
                idempotency_key=f"demo-ans-{self.scenario}-{rounds}",
                json={"answers": answers},
            )["data"]

        if snapshot["phase"] == "gate_failed":
            raise DemoError(f"门禁未通过：{snapshot.get('last_error')}")
        if snapshot["phase"] != "ready_to_freeze":
            raise DemoError(f"生成结束但阶段是 {snapshot['phase']}：{snapshot.get('last_error')}")
        return pipeline_id

    @staticmethod
    def _answer(questions: list[dict[str, Any]]) -> dict[str, str]:
        """自动答题：**优先取第一个选项**，没有选项就给一个保守的文本值。

        为什么能自动答：澄清问卷的每个问题都带 ``options``（生成节点从 plan 的候选值里
        给的），而演示要的是「一份能被批准的方案」而不是某个特定取值。真正需要人拍板
        的字段（目标表名、增量列）已经在需求原文里写清楚了，问卷里的选项就是它们的候选。
        """
        answers: dict[str, str] = {}
        for question in questions:
            options = question.get("options") or []
            if options:
                answers[question["key"]] = options[0]["value"]
            elif question.get("kind") == "threshold":
                meta = question.get("meta") or {}
                answers[question["key"]] = str(meta.get("min") or 0)
            else:
                answers[question["key"]] = question.get("hint") or "按需求原文执行"
        return answers

    def freeze(self, pipeline_id: int) -> tuple[int, dict[str, Any]]:
        versions = self._call(
            "GET", f"/api/v1/pipelines/{pipeline_id}/versions"
        )["data"]["list"]
        if not versions:
            raise DemoError(f"流水线 {pipeline_id} 没有任何版本，生成链路没落库？")
        version_id = int(versions[0]["id"])
        gate = self._call("GET", f"/api/v1/pipeline-versions/{version_id}/gate")["data"]
        print(f"    · 版本 {version_id} 门禁结论：{gate.get('conclusion')}")
        manifest = self._call(
            "POST", f"/api/v1/pipeline-versions/{version_id}/freeze",
            idempotency_key=f"demo-freeze-{version_id}",
            json={"note": f"demo_run 场景 {self.scenario}"},
        )["data"]
        print(f"    ✓ 已冻结 sha256={str(manifest.get('sha256'))[:16]}…")
        return version_id, manifest

    # ------------------------------------------------------------ 治理链路

    def prepare(self, version_id: int) -> int:
        detail = self._call(
            "POST", "/api/v1/preparations",
            idempotency_key=f"demo-prep-{version_id}",
            json={"pipeline_version_id": version_id, "project_id": self.project_id,
                  "note": f"demo_run 场景 {self.scenario}"},
        )["data"]
        prep_id = int(detail["id"])
        print(f"    · 准备单 {prep_id}：风险 {detail['risk_level']} "
              f"需要槽位 {list(detail['required_slots'])}")
        scope = detail.get("resource_scope") or {}
        print(f"      读：{[r['table'] for r in scope.get('reads', [])]} "
              f"写：{[w['table'] for w in scope.get('writes', [])]}")
        return prep_id

    def approve(self, prep_id: int, *, role: str, slot: str) -> None:
        self._call(
            "POST", f"/api/v1/preparations/{prep_id}/decisions",
            role=role,
            idempotency_key=f"demo-{slot}-{prep_id}",
            json={"slot": slot, "decision": "approve",
                  "comment": f"demo_run：{slot} 通过（{USERS[role]}）"},
        )
        print(f"    ✓ {slot} 已批准（{USERS[role]}）")

    def commit(self, prep_id: int) -> int:
        run = self._call(
            "POST", "/api/v1/runs",
            role="operator",
            idempotency_key=f"demo-run-{prep_id}",
            json={"preparation_id": prep_id},
        )["data"]
        print(f"    · 已提交运行 {run['id']}（影子表 {run.get('shadow_table')}）")
        return int(run["id"])

    def wait_run(self, run_id: int, timeout: float = 300.0) -> dict[str, Any]:
        """等运行走到终态。收尾由 beat 的扫描驱动，所以这里必然要轮询。"""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            detail = self._call("GET", f"/api/v1/runs/{run_id}", role="operator")["data"]
            status = detail["status"]
            if status != last:
                print(f"    · 运行状态：{status}")
                last = status
            if status in {"succeeded", "failed", "cancelled", "interrupted", "rolled_back"}:
                return detail
            time.sleep(3)
        raise DemoError(f"运行 {run_id} 在 {timeout:.0f} 秒内没有到终态（最后状态 {last}）")

    def report(self, run_id: int) -> None:
        for label, path in (("质量报告", f"/api/v1/runs/{run_id}/quality-report"),
                            ("发布记录", f"/api/v1/runs/{run_id}/publish")):
            try:
                data = self._call("GET", path, role="operator")["data"]
            except DemoError as exc:
                print(f"    · {label}：暂不可用（{exc}）")
                continue
            print(f"    · {label}：{_compact(data)}")

    def evidence(self) -> None:
        data = self._call(
            "GET", "/api/v1/governance/evidence", role="auditor",
            params={"project_id": self.project_id, "page": 1, "page_size": 8},
        )["data"]
        total = data.get("total")
        print(f"    · 证据账本共 {total} 条，最近 3 条：")
        for item in (data.get("list") or [])[:3]:
            print(f"        #{item.get('seq')} {item.get('event_type')} "
                  f"hash={str(item.get('event_hash'))[:12]}…")


def _compact(data: Any, limit: int = 240) -> str:
    text = repr(data)
    return text if len(text) <= limit else text[:limit] + "…"


def _project_id(demo: Demo) -> int:
    projects = demo._call("GET", "/api/v1/projects")["data"]
    for item in projects.get("list") or []:
        if item["code"] == PROJECT_CODE:
            return int(item["id"])
    raise DemoError(f"租户 {TENANT} 下没有项目 {PROJECT_CODE}（先跑 scripts/bootstrap_tenant.py）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="演示链路一键跑通（场景一 / 二）")
    parser.add_argument("--scenario", type=int, choices=[1, 2], default=1)
    parser.add_argument("--target", default=os.environ.get("ELT_DEMO_TARGET", "doris"),
                        choices=["doris", "mysql"])
    parser.add_argument("--requirement", default=None, help="覆盖需求原文（调试用）")
    args = parser.parse_args(argv)

    password = os.environ.get("ELT_DEMO_PASSWORD")
    if not password:
        sys.stderr.write("缺少 ELT_DEMO_PASSWORD（口径见本文件头部说明）\n")
        return 2

    from core.config import settings  # 只在需要凭据时引入，避免无谓地加载全部配置

    demo = Demo(args.scenario, args.target, 0, password)
    print(f"[0/6] 登录（租户 {TENANT}，API {API_BASE}）")
    demo.login_all()
    demo.project_id = _project_id(demo)
    print(f"    ✓ 项目 {PROJECT_CODE} id={demo.project_id}")

    print("[1/6] 数据源：源库 MySQL biz_demo")
    source_id = demo.ensure_datasource(
        name="演示源库-MySQL", ds_type="mysql",
        endpoint={"host": "127.0.0.1", "port": settings.DEMO_MYSQL_PORT,
                  "database": settings.DEMO_MYSQL_DB, "charset": "utf8mb4",
                  # 数据面视角：SeaTunnel 在 elt_net 网里，看到的是容器名与容器内端口
                  "exec_host": "mysql", "exec_port": 3306},
        credentials={"username": settings.DEMO_MYSQL_USER,
                     "password": settings.DEMO_MYSQL_PASSWORD.get_secret_value()},
        probe=True,
    )

    print(f"[2/6] 数据源：目标库 {args.target} dw_demo")
    if args.target == "doris":
        target_id = demo.ensure_datasource(
            name="演示目标库-Doris", ds_type="doris",
            endpoint={"host": "127.0.0.1", "port": 9030, "database": "dw_demo",
                      "exec_host": "doris", "exec_port": 9030},
            credentials={"username": settings.DEMO_DORIS_USER,
                         "password": settings.DEMO_DORIS_PASSWORD.get_secret_value()},
            probe=True,
        )
    else:
        target_id = demo.ensure_datasource(
            name="演示目标库-MySQL", ds_type="mysql",
            endpoint={"host": "127.0.0.1", "port": settings.DEMO_MYSQL_PORT,
                      "database": "dw_demo", "charset": "utf8mb4",
                      "exec_host": "mysql", "exec_port": 3306},
            credentials={"username": settings.DEMO_MYSQL_USER,
                         "password": settings.DEMO_MYSQL_PASSWORD.get_secret_value()},
            probe=True,
        )

    requirement = args.requirement or REQUIREMENTS[args.scenario]
    print(f"[3/6] 生成：{requirement}")
    pipeline_id = demo.generate(source_id, target_id, requirement)
    print(f"    ✓ 流水线 {pipeline_id} 生成完成")

    print("[4/6] 门禁与冻结")
    version_id, _manifest = demo.freeze(pipeline_id)

    print("[5/6] 准备单与四眼审批")
    prep_id = demo.prepare(version_id)
    demo.approve(prep_id, role="checker_data", slot="checker_data")
    demo.approve(prep_id, role="checker_security", slot="checker_security")

    print("[6/6] 提交执行（outbox → worker → SeaTunnel）")
    run_id = demo.commit(prep_id)
    detail = demo.wait_run(run_id)
    print(f"    ✓ 运行 {run_id} 终态：{detail['status']}")
    print(f"      指标：{_compact(detail.get('metrics'))}")
    if detail.get("error_message"):
        print(f"      错误：{detail['error_message'][:300]}")
    if detail.get("failure_snapshot"):
        print(f"      失败快照：{_compact(detail['failure_snapshot'])}")
    demo.report(run_id)
    demo.evidence()

    if detail["status"] != "succeeded":
        print(f"\n链路未走通：运行终态 {detail['status']}")
        return 1
    print("\n场景一/二 跑通：生成 → 门禁 → 冻结 → 双审批 → 执行 → 收尾 → 发布")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
