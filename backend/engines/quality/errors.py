"""质量契约的领域异常（be-17 §3.2）。

``ContractError`` 继承 ``ValueError`` 而**不继承** ``core.errors.AppError``：
``core/errors.py`` 里 import 了 fastapi，而 ``engines/`` 是纯函数层（CLAUDE.md §4.3），
继承它等于让「契约编译」这个纯算法把 Web 框架拖进依赖图——单测会因此需要 ASGI 环境，
而 ``engines`` 本来应该能在「没有 PG、没有 Redis、没有网络」的机器上跑。

翻译责任在上层：``services/quality.py`` 把它映射成 ``GATE_BLOCKED``（生成期，
版本不允许冻结）或 ``INTERNAL_ERROR``（运行期，冻结制品异常，正常流程不可达）。
"""

from __future__ import annotations


class ContractError(ValueError):
    """质量契约非法：**编译期**就拒绝。

    为什么要在编译期拦：这些参数会直接变成下推到数据面的 SQL。一份「拼得出来但判错」
    的 SQL 不会报任何错——它只是把不该拒的行拒掉、或者对违规行放行，
    症状是质量报告显示一切正常而错误表里空空如也。

    ``rule_index`` / ``rule_type`` 是为了**可定位**：契约来自 LLM 生成的 Plan，
    报「第 3 条 type_cast 规则非法」比报「契约非法」有用一个量级；
    生成期的自动修复也靠它知道该改哪一条。

    消息里只放字段名与参数形态，**不放字段值**（本模块本来就接触不到业务数据），
    因此这条消息可以安全进日志。
    """

    def __init__(
        self,
        message: str,
        *,
        rule_index: int | None = None,
        rule_type: str | None = None,
    ) -> None:
        self.rule_index = rule_index
        self.rule_type = rule_type
        location = ""
        if rule_index is not None:
            location = f"（rules[{rule_index}]"
            location += f"，rule_type={rule_type}）" if rule_type else "）"
        super().__init__(f"{message}{location}")
