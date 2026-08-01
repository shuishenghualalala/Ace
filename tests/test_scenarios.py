"""场景推荐（scenarios）模块测试：加载、推荐、绑定解析、HTTP 路由。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.gateway.routers.scenarios import create_scenarios_router
from crew.scenarios import (
    get_intro_lines,
    get_loading_statuses,
    get_scenarios,
    recommend,
    recommend_intro_lines,
    recommend_loading_statuses,
    resolve_binding,
)


def test_get_scenarios_loaded():
    scenarios = get_scenarios()
    assert isinstance(scenarios, list)
    assert scenarios, "内置 scenarios.yaml 至少应有一个场景"
    for s in scenarios:
        assert s.get("id")
        assert s.get("title")
        assert isinstance(s.get("items", []), list)


def test_recommend_count_and_subset():
    all_ids = {s["id"] for s in get_scenarios()}
    rec = recommend(2)
    assert len(rec) <= 2
    assert {s["id"] for s in rec} <= all_ids


def test_recommend_over_total_returns_all():
    total = len(get_scenarios())
    rec = recommend(total + 5)
    assert len(rec) == total


def test_get_intro_lines_loaded():
    lines = get_intro_lines()
    assert lines
    assert all(isinstance(line, str) and line.strip() for line in lines)


def test_recommend_intro_lines_count_and_subset():
    all_lines = set(get_intro_lines())
    rec = recommend_intro_lines(3)
    assert len(rec) <= 3
    assert set(rec) <= all_lines


def test_get_loading_statuses_loaded():
    statuses = get_loading_statuses()
    assert statuses
    assert all(isinstance(status, str) and status.strip() for status in statuses)


def test_recommend_loading_statuses_count_and_subset():
    all_statuses = set(get_loading_statuses())
    rec = recommend_loading_statuses(3)
    assert len(rec) <= 3
    assert set(rec) <= all_statuses


def test_resolve_binding_known_item():
    # 取第一个含 items 的场景的第一个细分玩法
    sub_id = None
    for s in get_scenarios():
        if s.get("items"):
            sub_id = s["items"][0]["id"]
            break
    assert sub_id is not None
    binding = resolve_binding(sub_id)
    assert binding is not None
    assert set(binding) >= {"skills", "inject", "mode"}
    assert isinstance(binding["skills"], list)


def test_resolve_binding_unknown_returns_none():
    assert resolve_binding("__no_such_sub__") is None
    assert resolve_binding("") is None


def test_bundled_scenarios_do_not_require_removed_skills():
    for scenario in get_scenarios():
        for item in scenario.get("items", []):
            assert item.get("skills", []) == []


def test_scenarios_http_routes():
    app = FastAPI()
    app.include_router(create_scenarios_router(crew=None))
    client = TestClient(app)

    r = client.get("/api/scenarios?count=2")
    assert r.status_code == 200
    assert len(r.json()) <= 2

    r_all = client.get("/api/scenarios/all")
    assert r_all.status_code == 200
    assert len(r_all.json()) == len(get_scenarios())

    r_lines = client.get("/api/scenarios/intro-lines?count=3")
    assert r_lines.status_code == 200
    assert len(r_lines.json()) <= 3

    r_status = client.get("/api/scenarios/loading-status?count=3")
    assert r_status.status_code == 200
    assert len(r_status.json()) <= 3
