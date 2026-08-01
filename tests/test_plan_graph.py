"""PlanGraph 图模型与校验单元测试。"""

from __future__ import annotations

from crew.dynamickanban.models import PlanEdge, PlanNode, PlanResult
from crew.dynamickanban.plan_graph import PlanGraph


def test_from_plan_result_with_nodes_and_edges() -> None:
    plan = PlanResult(
        summary="图计划",
        nodes=[
            PlanNode(id="n1", title="需求", detail="调研需求", assignee="pm"),
            PlanNode(id="n2", title="设计", detail="架构设计", assignee="architect"),
            PlanNode(id="n3", title="实现", detail="写代码", assignee="coder"),
        ],
        edges=[PlanEdge("n1", "n2"), PlanEdge("n2", "n3")],
    )
    graph = PlanGraph.from_plan_result(plan, valid_roles=["pm", "architect", "coder"], default_role="coder")
    assert list(graph.nodes.keys()) == ["n1", "n2", "n3"]
    assert graph.parents_of("n3") == ["n2"]
    order = [n.id for n in graph.topological_order()]
    assert order.index("n1") < order.index("n2") < order.index("n3")


def test_from_plan_result_with_legacy_tasks() -> None:
    plan = PlanResult(
        summary="旧版计划",
        tasks=[
            {"title": "需求调研", "detail": "", "assignee": "pm", "parent_task_ids": []},
            {"title": "数据库设计", "detail": "", "assignee": "architect", "parent_task_ids": ["需求调研"]},
            {"title": "实现 API", "detail": "", "assignee": "coder", "parent_task_ids": ["task_2"]},
        ],
    )
    graph = PlanGraph.from_plan_result(plan, valid_roles=["pm", "architect", "coder"], default_role="coder")
    assert "task_1" in graph.nodes
    assert "task_2" in graph.nodes
    assert "task_3" in graph.nodes
    assert graph.parents_of("task_2") == ["task_1"]
    assert graph.parents_of("task_3") == ["task_2"]


def test_topological_order_breaks_cycles() -> None:
    plan = PlanResult(
        summary="有环",
        nodes=[
            PlanNode(id="a", title="A", assignee="coder"),
            PlanNode(id="b", title="B", assignee="coder"),
            PlanNode(id="c", title="C", assignee="coder"),
        ],
        edges=[PlanEdge("a", "b"), PlanEdge("b", "c"), PlanEdge("c", "a")],
    )
    graph = PlanGraph.from_plan_result(plan)
    graph.validate_and_fix()
    # 环被移除后应能产出与节点数相等的拓扑序
    order = graph.topological_order()
    assert len(order) == 3


def test_validate_and_fix_removes_self_loop_and_unknown_refs() -> None:
    plan = PlanResult(
        summary="自环+未知引用",
        nodes=[
            PlanNode(id="x", title="X", assignee="coder"),
            PlanNode(id="y", title="Y", assignee="coder"),
        ],
        edges=[PlanEdge("x", "x"), PlanEdge("ghost", "y"), PlanEdge("x", "y")],
    )
    graph = PlanGraph.from_plan_result(plan)
    graph.validate_and_fix()
    assert all(e.parent_id != e.child_id for e in graph.edges)
    assert "ghost" not in {e.parent_id for e in graph.edges}
    assert PlanEdge("x", "y") in graph.edges


def test_assignee_normalization() -> None:
    plan = PlanResult(
        summary="角色归一化",
        nodes=[
            PlanNode(id="n1", title="A", assignee="PM"),
            PlanNode(id="n2", title="B", assignee="unknown"),
            PlanNode(id="n3", title="C", assignee=""),
        ],
        edges=[],
    )
    graph = PlanGraph.from_plan_result(
        plan, valid_roles=["project_manager", "coder"], default_role="project_manager"
    )
    assert graph.nodes["n1"].assignee == "project_manager"
    assert graph.nodes["n2"].assignee == "project_manager"
    assert graph.nodes["n3"].assignee == "project_manager"


def test_empty_node_id_gets_generated() -> None:
    plan = PlanResult(
        summary="缺 id",
        nodes=[
            PlanNode(id="", title="A", assignee="coder"),
            PlanNode(id="", title="B", assignee="coder"),
        ],
        edges=[],
    )
    graph = PlanGraph.from_plan_result(plan)
    ids = list(graph.nodes.keys())
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(ids)
