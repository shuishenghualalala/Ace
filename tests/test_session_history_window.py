"""P1-1：会话历史窗口读取（load_window + /api/session/{id}/history）。

核心验收：任意分页大小下，逐页拼回的消息序列与全量投影（load）完全一致——
涵盖 fork 前缀续接与 rewind 弃尾两类链形态。
"""

from __future__ import annotations

from contextlib import closing

import pytest

from crew.core.types import Message, ToolCall
from crew.state.session_store import SessionEventLogError, SQLiteSessionStore

OWNER = "A:uid-a"


def _save_turns(store: SQLiteSessionStore, sid: str, turns: int) -> None:
    msgs: list[Message] = []
    for i in range(turns):
        msgs.append(Message.user(f"q{i}"))
        msgs.append(Message.assistant(f"a{i}"))
    store.save(sid, msgs, owner_account_id=OWNER)


def _page_all(
    store: SQLiteSessionStore,
    sid: str,
    limit: int,
) -> tuple[list[tuple[str, Message]], int]:
    """逐页翻到底，返回 (按时间正序拼接的 (sid, msg) 序列, 页数)。

    页序天然从新到旧（尾窗 → 更早），拼回全量时需整体反转。
    """
    pages_buffer: list[list[tuple[str, Message]]] = []
    before: str | None = None
    pages = 0
    while True:
        messages, has_more, next_before = store.load_window(
            sid, owner_account_id=OWNER, limit=limit, before=before
        )
        pages_buffer.append([(sid_of, msg) for sid_of, _seq, msg in messages])
        pages += 1
        if not has_more:
            assert next_before is None
            break
        assert next_before is not None
        before = next_before
        assert pages < 100, "翻页未收敛"
    collected: list[tuple[str, Message]] = []
    for page in reversed(pages_buffer):
        collected.extend(page)
    return collected, pages


# ---- 基础：尾窗与翻页等价性 ----


def test_window_paging_equals_full_load(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "s1", turns=30)  # 60 条消息
        full = [( "s1", m) for m in store.load("s1", owner_account_id=OWNER)]
        for limit in (7, 20, 59, 60, 100):
            collected, _pages = _page_all(store, "s1", limit)
            assert [(sid, m.content) for sid, m in collected] == [
                (sid, m.content) for sid, m in full
            ], f"limit={limit} 分页拼回与全量不一致"
    finally:
        store.close()


def test_window_first_page_is_tail_window(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "s1", turns=10)  # 20 条：seq 1..20
        messages, has_more, next_before = store.load_window(
            "s1", owner_account_id=OWNER, limit=5
        )
        # 尾窗 = seq 16..20 升序
        assert [m.content for _sid, _seq, m in messages] == ["a7", "q8", "a8", "q9", "a9"]
        assert has_more is True
        assert next_before == "s1:16"
    finally:
        store.close()


def test_window_cursor_rejects_foreign_session(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "s1", turns=2)
        _save_turns(store, "other", turns=2)
        with pytest.raises(SessionEventLogError):
            store.load_window(
                "s1", owner_account_id=OWNER, limit=10, before="other:2"
            )
    finally:
        store.close()


# ---- fork 前缀续接 ----


def test_window_continues_into_fork_source(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "src", turns=10)  # 20 条：seq 1..20
        # 在 seq=10（a4 落点）处 fork；fork 自身再追加 3 条
        new_id = store.fork("src", 10, owner_account_id=OWNER)
        fork_msgs = store.load(new_id, owner_account_id=OWNER)
        fork_msgs += [
            Message.user("fq1"),
            Message.assistant("fa1"),
            Message.user("fq2"),
        ]
        store.save(new_id, fork_msgs, owner_account_id=OWNER)

        full = [(new_id if i >= 10 else "src", m) for i, m in enumerate(
            store.load(new_id, owner_account_id=OWNER)
        )]
        for limit in (3, 8, 50):
            collected, _pages = _page_all(store, new_id, limit)
            assert [(sid, m.content) for sid, m in collected] == [
                (sid, m.content) for sid, m in full
            ], f"limit={limit} fork 分页拼回与全量不一致"
    finally:
        store.close()


def test_window_cursor_crosses_fork_boundary(tmp_path):
    """首页不足时游标必须落到源会话上（sid:seq），后续页原样回传仍能继续。"""
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "src", turns=5)
        new_id = store.fork("src", 6, owner_account_id=OWNER)
        fork_msgs = store.load(new_id, owner_account_id=OWNER) + [
            Message.user("fq1"),
            Message.assistant("fa1"),
        ]
        store.save(new_id, fork_msgs, owner_account_id=OWNER)
        messages, has_more, next_before = store.load_window(
            new_id, owner_account_id=OWNER, limit=3
        )
        assert [m.content for _sid, _seq, m in messages][-2:] == ["fq1", "fa1"]
        assert has_more is True
        # 最旧一条来自源会话 → 游标指向源会话
        assert next_before is not None and next_before.startswith("src:")
        # 越权校验：源会话游标用于兄弟会话必须被拒
        with pytest.raises(SessionEventLogError):
            store.load_window("unrelated", owner_account_id=OWNER, limit=3, before=next_before)
    finally:
        store.close()


def test_window_random_fork_rewind_chains_paging_parity(tmp_path):
    """种子随机链 fuzz（固化测试人员 790/790 结论）：随机 save/fork/rewind 序列下，
    任意 limit 的逐页拼回必须与全量投影完全一致。"""
    import random

    def _role_event_seqs(store: SQLiteSessionStore, sid: str) -> list[int]:
        cursor = store._read_event_cursor(OWNER, sid)
        if cursor is None:
            return []
        # 只取物理归属本会话的事件 seq：fork 会话的链走行会穿过 end_seed
        # 混入源会话命名空间的 seq，拿去 rewind/fork 本会话是非法切口
        return [
            seq
            for s, seq, _m in store._walk_chain_window(OWNER, sid, cursor[0], None)
            if s == sid
        ]

    for seed in range(24):
        rng = random.Random(seed)
        store = SQLiteSessionStore(str(tmp_path / f"fuzz-{seed}.db"))
        try:
            store.save("root", [Message.user("m0")], owner_account_id=OWNER)
            live: list[str] = ["root"]
            turn = 0
            for _ in range(14):
                op = rng.random()
                sid = rng.choice(live)
                msgs = store.load(sid, owner_account_id=OWNER)
                seqs = _role_event_seqs(store, sid)
                if op < 0.6 or len(seqs) < 2:
                    turn += 1
                    store.save(
                        sid,
                        msgs + [Message.user(f"q{turn}"), Message.assistant(f"a{turn}")],
                        owner_account_id=OWNER,
                    )
                elif op < 0.8 and seqs:
                    # fork：边界取链上真实角色事件 seq（rewind 后 seq 与消息数错位）
                    child = store.fork(sid, rng.choice(seqs), owner_account_id=OWNER)
                    live.append(child)
                elif seqs:
                    # rewind 到某个非末位的链上事件（保留 ≥1 条消息）
                    store.rewind(sid, rng.choice(seqs[:-1]), owner_account_id=OWNER)

            for target in live:
                full = store.load(target, owner_account_id=OWNER)
                if not full:
                    continue
                for limit in (1, 2, 3, 7, 50):
                    collected, _pages = _page_all(store, target, limit)
                    # 只比内容与顺序：fork 续页的旧消息物理归属源会话（sid 不同是正确的）
                    assert [m.content for _sid, m in collected] == [
                        m.content for m in full
                    ], f"seed={seed} sid={target} limit={limit} 分页与全量不一致"
        finally:
            store.close()


def test_window_continues_after_source_rewind_below_fork_boundary(tmp_path):
    """F1 回归：fork 之后源会话 rewind 到 fork 边界之下，翻页不得跳段。

    游标落在祖先会话时 head 必须取游标位置本身；旧实现取 min(bound, leaf)，
    会被回退后的 leaf 夹低，跳过边界与游标之间的整段消息。
    """
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "src", turns=6)  # 12 条：seq 1..12
        child = store.fork("src", 12, owner_account_id=OWNER)
        store.save(
            child,
            store.load(child, owner_account_id=OWNER)
            + [Message.user("cq1"), Message.assistant("ca1")],
            owner_account_id=OWNER,
        )
        # fork 之后源会话回退到 seq=2：fork 前缀（1..12）行仍在，src 的 leaf=2
        store.rewind("src", 2, owner_account_id=OWNER)

        full = store.load(child, owner_account_id=OWNER)  # 14 条
        assert len(full) == 14
        collected, _pages = _page_all(store, child, limit=1)
        contents = [m.content for _sid, m in collected]
        assert contents == [m.content for m in full]
        # 显式断言被旧实现跳过的中段（q2..a5，即 seq 3..11）都在
        for expected in ("q2", "a2", "q3", "a3", "q4", "a4", "q5", "a5"):
            assert expected in contents
    finally:
        store.close()


# ---- rewind 弃尾排除 ----


def test_window_excludes_rewind_abandoned_tail(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        _save_turns(store, "s1", turns=5)  # 10 条：seq 1..10
        store.rewind("s1", 6, owner_account_id=OWNER)  # 回退到 seq=6（q3 落点）
        store.save(
            "s1",
            store.load("s1", owner_account_id=OWNER)
            + [Message.user("rq1"), Message.assistant("ra1")],
            owner_account_id=OWNER,
        )
        full = store.load("s1", owner_account_id=OWNER)
        collected, _pages = _page_all(store, "s1", 4)
        assert [m.content for _sid, m in collected] == [m.content for m in full]
        # rewind 弃尾（q4/a4/q5/a5）不得混入
        contents = [m.content for _sid, m in collected]
        assert "a4" not in contents and "q5" not in contents
        assert contents[-2:] == ["rq1", "ra1"]
    finally:
        store.close()


# ---- 端点冒烟 ----


@pytest.mark.asyncio
async def test_history_window_endpoint_envelope_and_parity(tmp_path, auth_headers):
    from httpx import ASGITransport, AsyncClient

    from crew.app import build_app
    from crew.gateway.server import create_app
    from crew.state.config import Config

    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    msgs: list[Message] = []
    for i in range(8):
        msgs.append(Message.user(f"q{i}"))
        msgs.append(Message.assistant(f"a{i}"))
    crew.session_store.save("s1", msgs, owner_account_id=OWNER)
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        first = await client.get("/api/session/s1/history", params={"limit": 5})
        assert first.status_code == 200
        body = first.json()
        assert set(body.keys()) == {"items", "has_more", "next_before", "mode", "revision", "head"}
        assert body["mode"] == "snapshot"
        # Move the requested five-row cut back to the start of its oldest turn.
        assert [item["content"] for item in body["items"]] == ["q5", "a5", "q6", "a6", "q7", "a7"]
        assert body["has_more"] is True
        assert body["next_before"].endswith(":s1:11")

        second = await client.get(
            "/api/session/s1/history", params={"limit": 50, "before": body["next_before"], "revision": body["revision"]}
        )
        assert second.status_code == 200
        body2 = second.json()
        assert [item["content"] for item in body2["items"]] == [
            "q0", "a0", "q1", "a1", "q2", "a2", "q3", "a3", "q4", "a4",
        ]
        assert body2["mode"] == "page"
        assert (body2["revision"], body2["head"]) == (body["revision"], body["head"])
        assert body2["has_more"] is False
        assert body2["next_before"] is None

        # 与旧全量端点的 items 全等（含顺序）
        legacy = await client.get("/api/session/s1")
        assert legacy.status_code == 200
        projected = body2["items"] + body["items"]
        assert len({item["history_id"] for item in projected}) == len(projected)
        assert [{k: v for k, v in item.items() if k != "history_id"} for item in projected] == legacy.json()

        bad = await client.get("/api/session/s1/history", params={"before": "oops"})
        assert bad.status_code == 400

        crew.session_store.save("s1", [*msgs, Message.user("q8"), Message.assistant("a8")], owner_account_id=OWNER)
        suffix = await client.get("/api/session/s1/history", params={"after": body["head"], "revision": body["revision"]})
        assert suffix.status_code == 200
        assert suffix.json()["mode"] == "append"
        assert [item["content"] for item in suffix.json()["items"]] == ["q8", "a8"]
    crew.session_store.close()


def _versioned(store, sid="s1", limit=2, **options):
    from crew.gateway.routers.sessions import read_session_history_window

    return read_session_history_window(store, sid, OWNER, limit, **options)


def test_versioned_history_replays_complete_append_then_preserves_older_cursor(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        _save_turns(store, "s1", 4)
        first = _versioned(store)
        previous_ids = [item["history_id"] for item in first["items"]]
        _save_turns(store, "s1", 5)
        added = _versioned(store, after=first["head"], revision=first["revision"])
        assert added["mode"] == "append"
        assert added["revision"] == first["revision"] and added["head"] != first["head"]
        assert [item["content"] for item in added["items"]] == ["q4", "a4"]
        assert not added["has_more"] and added["next_before"] is None
        current_page = _versioned(store, limit=4)
        assert [item["history_id"] for item in current_page["items"][:2]] == previous_ids
        quiet = _versioned(store, after=added["head"], revision=added["revision"])
        assert quiet["mode"] == "append" and quiet["items"] == []
        older = _versioned(store, before=first["next_before"], revision=first["revision"])
        assert older["mode"] == "page"
        assert [item["content"] for item in older["items"]] == ["q2", "a2"]

        _save_turns(store, "s1", 8)
        overflow = _versioned(store, after=first["head"], revision=first["revision"])
        assert overflow["mode"] == "snapshot"
        assert [item["content"] for item in overflow["items"]] == ["q7", "a7"]


def test_versioned_history_rejects_old_incarnation_and_rewound_cursor(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        _save_turns(store, "s1", 4)
        old = _versioned(store)
        store.rewind("s1", 4, owner_account_id=OWNER)
        _save_turns(store, "s1", 3)  # Append an alternative tail after the rewind.
        for options in ({"before": old["next_before"]}, {"after": old["head"]}):
            fresh = _versioned(store, revision=old["revision"], **options)
            assert fresh["mode"] == "snapshot" and fresh["revision"] != old["revision"]
            assert [item["content"] for item in fresh["items"]] == ["q2", "a2"]
        store.clear("s1", owner_account_id=OWNER)
        _save_turns(store, "s1", 4)
        new = _versioned(store)
        assert old["revision"] != new["revision"]
        assert {item["history_id"] for item in old["items"]}.isdisjoint(item["history_id"] for item in new["items"])


def test_versioned_fork_retains_frozen_ancestor_after_rewind_but_not_rewrite(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        _save_turns(store, "src", 5)
        forked = store.fork("src", 6, owner_account_id=OWNER)
        first = _versioned(store, forked)
        store.rewind("src", 2, owner_account_id=OWNER)
        older = _versioned(store, forked, before=first["next_before"], revision=first["revision"])
        assert older["mode"] == "page" and older["revision"] == first["revision"]
        assert [(item["source_session_id"], item["content"]) for item in older["items"]] == [("src", "q1"), ("src", "a1")]
        rewritten = [Message.user(f"replacement {index}") for index in range(8)]
        store.save("src", rewritten, owner_account_id=OWNER)
        newer = _versioned(store, forked, before=first["next_before"], revision=first["revision"])
        assert newer["mode"] == "snapshot" and newer["revision"] != first["revision"]


def test_history_window_pairs_tools_at_a_page_boundary_and_reprojects_unsafe_append(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        path = "/tmp/.crew/accounts/acct_0123456789abcdef/task_workspaces/default/downloads/browser/shot.png"
        partial = [Message.user("Capture"), Message.assistant("", [ToolCall("shot", "browser_use", {"action": "screenshot"})])]
        store.save("s1", partial, owner_account_id=OWNER)
        before_tool = _versioned(store)
        full = [*partial, Message.tool("shot", path, name="browser_use"), Message.assistant("Captured")]
        store.save("s1", full, owner_account_id=OWNER)
        recovered = _versioned(store, limit=5, after=before_tool["head"], revision=before_tool["revision"])
        assert recovered["mode"] == "snapshot" and recovered["items"][1]["tool_calls"][0]["result"] == path

        store.save("s1", [*full, Message.assistant("Summary")], owner_account_id=OWNER)
        adjacent = _versioned(store, limit=5, after=recovered["head"], revision=recovered["revision"])
        assert adjacent["mode"] == "snapshot"
        store.save("s1", [*full, Message.assistant("Summary"), Message.user("Next"), Message.assistant("Done")], owner_account_id=OWNER)
        latest = _versioned(store)
        older = _versioned(store, limit=3, before=latest["next_before"], revision=latest["revision"])
        assert [item["content"] for item in older["items"]] == ["Capture", "", "Captured", "Summary"]
        assert older["items"][1]["tool_calls"][0]["result"] == path


def test_versioned_history_keeps_rows_and_revision_in_one_wal_read_snapshot(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crew.db")
    with closing(SQLiteSessionStore(db_path)) as writer, closing(SQLiteSessionStore(db_path)) as reader:
        _save_turns(writer, "s1", 4)
        prior = _versioned(reader)
        fetch = reader._fetch_rows_desc

        def rewrite_during_fetch(*args):
            writer.save("s1", [Message.user("new"), Message.assistant("answer")], owner_account_id=OWNER)
            monkeypatch.setattr(reader, "_fetch_rows_desc", fetch)
            return fetch(*args)

        monkeypatch.setattr(reader, "_fetch_rows_desc", rewrite_during_fetch)
        assert _versioned(reader) == prior
        later = _versioned(reader)
        assert later["revision"] != prior["revision"]
        assert [item["content"] for item in later["items"]] == ["new", "answer"]


def test_versioned_history_reads_legacy_blob_before_backfill_and_invalidates_its_cursors(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteSessionStore, "_start_legacy_backfill", lambda _store: None)
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        legacy = [Message.user("old prompt"), Message.assistant("old answer"), Message.user("latest"), Message.assistant("reply")]
        store.transaction(lambda conn: conn.execute(
            "INSERT INTO sessions(session_id, owner_account_id, messages, updated_at) VALUES (?, ?, ?, ?)",
            ("s1", OWNER, store._dump(legacy), 1.0),
        ))
        first = _versioned(store)
        assert [item["content"] for item in first["items"]] == ["latest", "reply"]
        older = _versioned(store, before=first["next_before"], revision=first["revision"])
        assert [item["content"] for item in older["items"]] == ["old prompt", "old answer"]

        assert store.transaction(store._backfill_legacy_blobs) == 1
        modern = _versioned(store, before=first["next_before"], revision=first["revision"])
        assert modern["mode"] == "snapshot" and modern["revision"] != first["revision"]
        assert [item["content"] for item in modern["items"]] == ["latest", "reply"]
        assert {item["history_id"] for item in first["items"]}.isdisjoint(item["history_id"] for item in modern["items"])
