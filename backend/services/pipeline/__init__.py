"""流程生成的 service 层（be-09 … be-12）。

五个模块，依赖方向是单向的：

    generation ─┐
    artifacts ──┼─→ gating ─→ engines.gate
                ├─→ repair ─→ engines.autofix + clients.llm
                ├─→ parts ───→ engines.fingerprint
                └─→ repository ─→ models

``repository`` 是唯一碰 ORM 的地方，``parts`` 是唯一定义指纹五部分的地方，
``gating`` 是唯一组装 ``GateContext`` 的地方。其余模块都只调它们。

**本文件刻意不 re-export 任何东西**：``import services.pipeline`` 之后
写 ``services.pipeline.artifacts`` 是明确的，而 ``from services.pipeline import list_pipelines``
会让人看不出这个函数属于哪一层。api 层照 ``from services.pipeline import artifacts as artifact_service``
引入，模块即边界。
"""
