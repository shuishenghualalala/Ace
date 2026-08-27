#!/usr/bin/env python3
"""三实例 mock-bus 端到端冒烟：建群/追加成员/改名+模式/退群/重启快照。

用法（无需蓝牙，走 TCP 模拟总线）：
    python3 tests/e2e/nearby_mock_bus_smoke.py

若 nearby/target/debug/crew-nearby 不存在会先执行 cargo build。
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = str(REPO_ROOT / "nearby" / "target" / "debug" / "crew-nearby")
if not os.path.exists(BIN):
    subprocess.run(["cargo", "build"], cwd=REPO_ROOT / "nearby", check=True)
ENDPOINT = "127.0.0.1:39211"
BASE = tempfile.mkdtemp(prefix="nearby-e2e-")
procs = []
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


class Instance:
    def __init__(self, name):
        self.name = name
        self.peer_id = f"ace_{name}"
        self.events = queue.Queue()
        self.seen = []
        state_dir = os.path.join(BASE, name)
        os.makedirs(state_dir, exist_ok=True)
        self.proc = subprocess.Popen(
            [BIN, "--transport", "mock", "--mock-endpoint", ENDPOINT, "--ipc",
             "--peer-id", self.peer_id, "--display-name", name.capitalize(),
             "--agent-name", f"{name}-agent", "--state-dir", state_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        procs.append(self.proc)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.events.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def send(self, cmd):
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    def wait_event(self, pred, timeout=10):
        for event in self.seen:
            if pred(event):
                return event
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=max(deadline - time.time(), 0.05))
            except queue.Empty:
                break
            self.seen.append(event)
            if pred(event):
                return event
        return None


try:
    bus = subprocess.Popen([BIN, "--transport", "mock", "--mock-bus", "--mock-endpoint", ENDPOINT],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(bus)
    time.sleep(0.5)

    alice = Instance("alice")
    bob = Instance("bob")
    carol = Instance("carol")
    for inst in (alice, bob, carol):
        ready = inst.wait_event(lambda e: e.get("type") == "ready")
        assert ready, f"{inst.name} no ready"
    alice.send({"type": "start_discovery"})

    # 等待互相发现，然后显式建连（mock 的自动连接不发 peer_connected 事件）
    time.sleep(2)
    for inst, other in ((alice, "ace_bob"), (alice, "ace_carol"),
                        (bob, "ace_alice"), (carol, "ace_alice")):
        inst.send({"type": "connect_peer", "peer_id": other})
    for inst, other in ((alice, "ace_bob"), (alice, "ace_carol"),
                        (bob, "ace_alice"), (carol, "ace_alice")):
        ev = inst.wait_event(
            lambda e, o=other: e.get("type") == "peer_connected" and e.get("peer", {}).get("peer_id") == o, 15)
        check(f"{inst.name} 连接 {other}", ev is not None)

    # 1. alice 建群（mention 模式），邀请 bob
    alice.send({"type": "create_room", "room_id": "room_x", "room_name": "项目群",
                "peer_ids": ["ace_bob"], "agent_mode": "mention"})
    ev = alice.wait_event(lambda e: e.get("type") == "room_created", 5)
    check("alice 建群(mention, owner=alice)",
          ev is not None and ev.get("agent_mode") == "mention" and ev.get("owner_peer_id") == "ace_alice",
          json.dumps(ev, ensure_ascii=False) if ev else "no room_created")
    ev = bob.wait_event(lambda e: e.get("type") == "room_joined" and e.get("room_id") == "room_x", 5)
    check("bob 收到邀请入群(owner=alice)",
          ev is not None and ev.get("owner_peer_id") == "ace_alice" and ev.get("room_name") == "项目群",
          json.dumps(ev, ensure_ascii=False) if ev else "no room_joined")

    # 2. alice 发一条群消息制造历史
    alice.send({"type": "send_room_message", "room_id": "room_x", "text": "第一条历史消息"})
    alice.wait_event(lambda e: e.get("type") == "message", 5)
    bob.wait_event(lambda e: e.get("type") == "message", 5)

    # 3. alice 用 invite_to_room 追加 carol
    alice.send({"type": "invite_to_room", "room_id": "room_x", "peer_ids": ["ace_carol"]})
    ev = carol.wait_event(lambda e: e.get("type") == "room_joined" and e.get("room_id") == "room_x", 5)
    check("carol 被追加邀请入群", ev is not None and ev.get("owner_peer_id") == "ace_alice",
          json.dumps(ev, ensure_ascii=False) if ev else "no room_joined")
    ev = bob.wait_event(
        lambda e: e.get("type") == "room_member_joined" and e.get("peer_id") == "ace_carol", 5)
    check("bob 收到 carol 加入事件", ev is not None,
          json.dumps(ev, ensure_ascii=False) if ev else "no room_member_joined")

    # 4. alice 同时改群名 + 触发模式
    alice.send({"type": "set_room_agent_mode", "room_id": "room_x",
                "agent_mode": "quiet", "room_name": "新群名"})
    for inst in (alice, bob, carol):
        ev = inst.wait_event(lambda e: e.get("type") == "room_settings_updated", 5)
        check(f"{inst.name} 收到 settings 更新(quiet+新群名)",
              ev is not None and ev.get("agent_mode") == "quiet" and ev.get("room_name") == "新群名",
              json.dumps(ev, ensure_ascii=False) if ev else "no room_settings_updated")

    # 5. 非群主不能邀请
    bob.send({"type": "invite_to_room", "room_id": "room_x", "peer_ids": ["ace_carol"]})
    ev = bob.wait_event(lambda e: e.get("type") == "error", 5)
    check("非群主邀请被拒绝", ev is not None and "群主" in ev.get("message", ""),
          json.dumps(ev, ensure_ascii=False) if ev else "no error")

    # 6. carol 退群
    carol.send({"type": "leave_room", "room_id": "room_x"})
    carol.wait_event(lambda e: e.get("type") == "room_left", 5)
    ev = bob.wait_event(
        lambda e: e.get("type") == "room_member_left" and e.get("peer_id") == "ace_carol", 5)
    check("bob 收到 carol 退群事件", ev is not None,
          json.dumps(ev, ensure_ascii=False) if ev else "no room_member_left")

    # 7. 重启 alice，验证快照
    alice.proc.stdin.close()
    alice.proc.wait(timeout=5)
    procs.remove(alice.proc)
    time.sleep(0.3)
    alice2 = Instance("alice")
    snap = alice2.wait_event(lambda e: e.get("type") == "history_snapshot", 5)
    room = None
    if snap:
        room = next((r for r in snap.get("rooms", []) if r.get("room_id") == "room_x"), None)
    check("重启后快照含 owner/新群名/quiet/历史消息",
          room is not None
          and room.get("owner_peer_id") == "ace_alice"
          and room.get("room_name") == "新群名"
          and room.get("agent_mode") == "quiet"
          and any(m.get("payload", {}).get("text") == "第一条历史消息" for m in room.get("messages", [])),
          json.dumps(room, ensure_ascii=False)[:300] if room else json.dumps(snap, ensure_ascii=False)[:300])

finally:
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(0.3)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    shutil.rmtree(BASE, ignore_errors=True)

failed = [r for r in results if not r[1]]
print(f"\n{'=' * 50}\n总计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
