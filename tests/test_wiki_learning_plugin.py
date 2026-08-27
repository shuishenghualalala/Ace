from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from crew.core.runctx import current_owner_account_id, current_request_id, current_session_id
from crew.core.types import Message, ToolCall
from crew.plugins.manager import PluginManager
from crew.state.config import Config
from crew.tools.registry import Registry
from crew.wiki.schemas import WikiPage
from crew.wiki.store._filesystem import FileSystemWikiStore
from plugins.wiki_learning.store import WikiLearningStore
from plugins.wiki_learning.tools import ASSESS_SCHEMA


@contextmanager
def _run_context(*, owner: str = "owner-a", session: str = "wiki-test", request: str = "req-1"):
    owner_token = current_owner_account_id.set(owner)
    session_token = current_session_id.set(session)
    request_token = current_request_id.set(request)
    try:
        yield
    finally:
        current_request_id.reset(request_token)
        current_session_id.reset(session_token)
        current_owner_account_id.reset(owner_token)


def _load_plugin(
    tmp_path: Path,
) -> tuple[Registry, PluginManager, Config, FileSystemWikiStore, str]:
    config = Config(db_path=str(tmp_path / "crew.db"))
    config.wiki.storage.root = str(tmp_path / "wiki")
    registry = Registry()
    manager = PluginManager(registry=registry, services={"config": config})
    plugin_dir = Path(__file__).parents[1] / "plugins" / "wiki_learning"
    manifest = manager._read_manifest(plugin_dir, key="wiki_learning", source="bundled")
    assert manifest is not None
    manager._load_plugin(manifest)
    wiki_store = FileSystemWikiStore(storage_root=config.wiki.storage.resolved_root())
    page = wiki_store.save_page(
        WikiPage(
            id="top_python",
            page_type="topic",
            title="Python 并发",
            content="线程适合阻塞 I/O；asyncio 通过事件循环组织协作式并发。",
            file_path="",
        ),
        owner_account_id="owner-a",
        kb_id="kb-study",
    )
    return registry, manager, config, wiki_store, page.id


def _messages(answer: str, kb_id: str = "kb-study") -> list[Message]:
    return [
        Message.user(answer),
        Message(
            role="user",
            content=f"当前活跃知识库（active_kb_id）：{kb_id}",
            is_meta=True,
            attachment_type="wiki_agent_context",
            attachment_data={"active_kb_id": kb_id},
        ),
    ]


def test_store_reuses_shared_db_and_keeps_own_schema(tmp_path):
    db_path = tmp_path / "crew.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE existing_ace_table (value TEXT)")
    conn.commit()
    conn.close()

    store = WikiLearningStore(db_path)
    episode = store.open_episode("owner-a", "wiki-a", "kb-a", goal="复习")
    assert episode["status"] == "active"
    store.close()

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "existing_ace_table" in tables
    assert "wiki_learning_episodes" in tables
    assert "wiki_learning_schema" in tables


def test_store_owner_and_session_isolation(tmp_path):
    store = WikiLearningStore(tmp_path / "crew.db")
    episode = store.open_episode("owner-a", "wiki-a", "kb-a", goal="A")
    assert store.get_episode(episode["id"], "owner-b", "wiki-a") is None
    assert store.get_episode(episode["id"], "owner-a", "wiki-b") is None
    assert store.active_episode("owner-b", "wiki-a", "kb-a") is None
    store.close()


@pytest.mark.asyncio
async def test_plugin_learning_flow_captures_answer_privately(tmp_path):
    registry, manager, config, wiki_store, page_id = _load_plugin(tmp_path)
    assert set(registry.names()) == {
        "wiki_learning_state",
        "wiki_learning_activity",
        "wiki_learning_assess",
    }
    assert all(registry.toolset_for(name) == "wiki.manage" for name in registry.names())
    assert "response_text" not in ASSESS_SCHEMA["parameters"]["properties"]

    with _run_context(request="req-open"):
        await manager.pre_llm_call("wiki-test", _messages("帮我复习 Python 并发"))
        opened = await registry.execute(
            ToolCall("call-open", "wiki_learning_state", {"action": "open", "goal": "面试复习"})
        )
    assert not opened.is_error
    episode_id = json.loads(opened.content)["episode"]["id"]

    with _run_context(request="req-update"):
        await manager.pre_llm_call("wiki-test", _messages("难一点，只练这个页面"))
        updated = await registry.execute(
            ToolCall(
                "call-update",
                "wiki_learning_state",
                {
                    "action": "update",
                    "episode_id": episode_id,
                    "constraints": {"difficulty": "hard"},
                    "page_ids": [page_id],
                },
            )
        )
    assert json.loads(updated.content)["episode"]["constraints"] == {"difficulty": "hard"}

    with _run_context(request="req-question"):
        await manager.pre_llm_call("wiki-test", _messages("请出一道题"))
        created = await registry.execute(
            ToolCall(
                "call-create",
                "wiki_learning_activity",
                {
                    "action": "create",
                    "episode_id": episode_id,
                    "activity_type": "interview",
                    "prompt": "线程与 asyncio 分别适合什么场景？",
                    "evidence_page_ids": [page_id],
                    "knowledge_keys": ["python.concurrency.choice"],
                },
            )
        )
    assert not created.is_error
    created_activity = json.loads(created.content)["activity"]
    activity_id = created_activity["id"]
    assert created_activity["public_payload"] == {
        "schema": "crew.interaction.v1",
        "interaction": {"kind": "text"},
    }

    raw_answer = "线程适合 I/O；asyncio 适合大量协作式 I/O。"
    with _run_context(request="req-answer"):
        await manager.pre_llm_call("wiki-test", _messages(raw_answer))
        assessed = await registry.execute(
            ToolCall(
                "call-assess",
                "wiki_learning_assess",
                {
                    "activity_id": activity_id,
                    "summary": "方向正确，能区分两种并发模型。",
                    "score": 0.85,
                    "strengths": ["场景判断正确"],
                    "gaps": ["可以补充调度模型"],
                    "knowledge_signals": {"python.concurrency.choice": 0.85},
                    "evidence_page_ids": [page_id],
                },
            )
        )
    assert not assessed.is_error
    payload = json.loads(assessed.content)
    assert raw_answer not in assessed.content
    assert payload["assessment"]["response_chars"] == len(raw_answer)
    assert payload["mastery"][0]["level"] == "proficient"

    conn = sqlite3.connect(config.db_path)
    stored_answer = conn.execute(
        "SELECT response_text FROM wiki_learning_assessments WHERE activity_id=?", (activity_id,)
    ).fetchone()[0]
    conn.close()
    assert stored_answer == raw_answer

    wiki_store.close()
    assert manager.unload_plugin("wiki_learning") is True
    assert registry.names() == []
    assert manager.plugin_skill_roots() == []


@pytest.mark.asyncio
async def test_assessment_is_idempotent_per_request_and_not_same_turn(tmp_path):
    registry, manager, _, wiki_store, page_id = _load_plugin(tmp_path)
    with _run_context(request="req-open"):
        await manager.pre_llm_call("wiki-test", _messages("考考我"))
        opened = await registry.execute(ToolCall("c1", "wiki_learning_state", {"action": "open"}))
    episode_id = json.loads(opened.content)["episode"]["id"]

    with _run_context(request="req-question"):
        await manager.pre_llm_call("wiki-test", _messages("出题"))
        created = await registry.execute(
            ToolCall(
                "c2",
                "wiki_learning_activity",
                {
                    "action": "create",
                    "episode_id": episode_id,
                    "activity_type": "quiz",
                    "prompt": "asyncio 的核心调度机制是什么？",
                    "evidence_page_ids": [page_id],
                    "knowledge_keys": ["python.asyncio.event_loop"],
                },
            )
        )
        activity_id = json.loads(created.content)["activity"]["id"]
        same_turn = await registry.execute(
            ToolCall(
                "c3",
                "wiki_learning_assess",
                {
                    "activity_id": activity_id,
                    "summary": "不应成功",
                    "score": 0,
                    "knowledge_signals": {"python.asyncio.event_loop": 0},
                },
            )
        )
    assert same_turn.is_error

    args = {
        "activity_id": activity_id,
        "summary": "正确",
        "score": 1,
        "knowledge_signals": {"python.asyncio.event_loop": 1},
    }
    with _run_context(request="req-answer"):
        await manager.pre_llm_call("wiki-test", _messages("事件循环"))
        first = await registry.execute(ToolCall("c4", "wiki_learning_assess", args))
        second = await registry.execute(ToolCall("c5", "wiki_learning_assess", args))
    assert not first.is_error
    assert not second.is_error
    assert (
        json.loads(first.content)["assessment"]["id"]
        == json.loads(second.content)["assessment"]["id"]
    )

    wiki_store.close()
    manager.unload_plugin("wiki_learning")


@pytest.mark.asyncio
async def test_activity_rejects_nested_private_answers(tmp_path):
    registry, manager, _, wiki_store, page_id = _load_plugin(tmp_path)
    with _run_context(request="req-open"):
        await manager.pre_llm_call("wiki-test", _messages("考考我"))
        opened = await registry.execute(ToolCall("c1", "wiki_learning_state", {"action": "open"}))
    episode_id = json.loads(opened.content)["episode"]["id"]

    with _run_context(request="req-question"):
        await manager.pre_llm_call("wiki-test", _messages("出一道选择题"))
        created = await registry.execute(
            ToolCall(
                "c2",
                "wiki_learning_activity",
                {
                    "action": "create",
                    "episode_id": episode_id,
                    "activity_type": "quiz",
                    "prompt": "asyncio 的核心调度机制是什么？",
                    "evidence_page_ids": [page_id],
                    "knowledge_keys": ["python.asyncio.event_loop"],
                    "public_payload": {
                        "schema": "crew.interaction.v1",
                        "interaction": {
                            "kind": "single_choice",
                            "options": [{"id": "A", "label": "事件循环"}],
                            "correct_answer": "A",
                        },
                    },
                },
            )
        )
    assert created.is_error
    assert "不能包含答案" in created.content

    wiki_store.close()
    manager.unload_plugin("wiki_learning")


@pytest.mark.asyncio
async def test_learning_skill_only_injects_for_relevant_wiki_turn(tmp_path):
    _, manager, _, wiki_store, _ = _load_plugin(tmp_path)
    with _run_context():
        unrelated = [Message.user("这个页面讲了什么？")]
        await manager.pre_llm_call("wiki-test", unrelated)
        assert not any("Wiki learning coach" in message.content for message in unrelated)

        learning = _messages("请根据知识库模拟面试")
        await manager.pre_llm_call("wiki-test", learning)
        assert any("Wiki learning coach" in message.content for message in learning)

        normal = _messages("请根据知识库模拟面试")
        await manager.pre_llm_call("session-normal", normal)
        assert not any("Wiki learning coach" in message.content for message in normal)

    wiki_store.close()
    manager.unload_plugin("wiki_learning")
