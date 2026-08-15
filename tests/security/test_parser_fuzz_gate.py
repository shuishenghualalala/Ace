"""Deterministic, bounded parser-property gate for security baseline TEST-012."""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

# ``crew.state.logging`` imports ``crew.tools.redact``. Initializing the tools
# package first avoids depending on whichever unrelated test happened to import
# that package before the Gateway parser.
import crew.tools  # noqa: F401
from crew.gateway.ws import (
    WS_MAX_FRAME_BYTES,
    WebSocketProtocolError,
    decode_ws_text_frame,
)
from crew.security.actions import normalize_exec_action
from crew.security.outbound import OutboundDenied, OutboundPolicy
from crew.security.runtime_client import ShellVerdict, _parse_classification
from crew.tools.file_utils import decode_local_file_uri

CORPUS_PATH = Path(__file__).with_name("test_012_parser_corpus.json")
ROOT = CORPUS_PATH.parents[2]
ABSOLUTE_MAX_CASES = 2048
ABSOLUTE_MAX_GENERATED_INPUT_BYTES = 4096


def _corpus() -> dict[str, Any]:
    value = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert value["schema_version"] == 1
    return value


def _campaign_seed(corpus: dict[str, Any]) -> int:
    return int(os.environ.get("ACE_TEST012_SEED", corpus["campaign"]["seed"]))


def _campaign_cases(corpus: dict[str, Any]) -> int:
    minimum = int(corpus["campaign"]["ci_cases"])
    maximum = int(corpus["campaign"]["max_cases"])
    assert 1 <= minimum <= maximum <= ABSOLUTE_MAX_CASES
    requested = int(os.environ.get("ACE_TEST012_CASES", minimum))
    return max(minimum, min(requested, maximum))


def _max_generated_input_bytes(corpus: dict[str, Any]) -> int:
    maximum = int(corpus["campaign"]["max_generated_input_bytes"])
    assert 1 <= maximum <= ABSOLUTE_MAX_GENERATED_INPUT_BYTES
    return maximum


def _random_parser_text(rng: random.Random, maximum: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}[],:;|&$`'\"/\\ \t\r\n"
    return "".join(rng.choice(alphabet) for _ in range(rng.randrange(maximum + 1)))


@pytest.fixture
def no_host_execution(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """Turn accidental process, shell, or network use by a parser into a test failure."""

    calls: list[tuple[str, tuple[Any, ...]]] = []

    def blocked(name: str):
        def fail(*args: Any, **_kwargs: Any) -> None:
            calls.append((name, args))
            raise AssertionError(f"parser attempted forbidden host operation: {name}")

        return fail

    monkeypatch.setattr(os, "system", blocked("os.system"))
    monkeypatch.setattr(subprocess, "Popen", blocked("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", blocked("subprocess.run"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked("create_subprocess_exec"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("socket.getaddrinfo"))
    return calls


def test_test012_gateway_frame_and_json_property_gate(
    no_host_execution: list[tuple[str, tuple[Any, ...]]],
) -> None:
    corpus = _corpus()
    maximum = _max_generated_input_bytes(corpus)
    rng = random.Random(_campaign_seed(corpus) ^ 0x4652414D45)

    for frame in corpus["frame"]["gateway_valid"]:
        decoded = decode_ws_text_frame(frame)
        assert isinstance(decoded, dict)
    for frame in corpus["frame"]["gateway_invalid"]:
        with pytest.raises(WebSocketProtocolError):
            decode_ws_text_frame(frame)
    with pytest.raises(WebSocketProtocolError, match="FRAME_TOO_LARGE"):
        decode_ws_text_frame("x" * (WS_MAX_FRAME_BYTES + 1))
    with pytest.raises(WebSocketProtocolError, match="BINARY_UNSUPPORTED"):
        decode_ws_text_frame(b'{"kind":"pong"}')

    base = json.loads(corpus["frame"]["gateway_valid"][0])
    for case_index in range(_campaign_cases(corpus)):
        value = dict(base)
        mutation = rng.randrange(6)
        if mutation == 0:
            value.pop("protocol_version")
        elif mutation == 1:
            value["client_sequence"] = True
        elif mutation == 2:
            value["nonce"] = ["nested"]
        elif mutation == 3:
            value["nonce"] = rng.choice(["short", "bad nonce", "\x00" * 16])
        elif mutation == 4:
            value["unknown_" + str(case_index)] = True
        else:
            value["kind"] = rng.choice(["", "message", 7])
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        assert len(encoded.encode("utf-8")) <= maximum
        with pytest.raises(WebSocketProtocolError):
            decode_ws_text_frame(encoded)

        arbitrary = _random_parser_text(rng, maximum)
        assert len(arbitrary.encode("utf-8")) <= maximum
        try:
            parsed = decode_ws_text_frame(arbitrary)
        except WebSocketProtocolError:
            pass
        else:
            assert isinstance(parsed, dict), f"case {case_index} returned a non-object"

    nested: object = "leaf"
    for _ in range(14):
        nested = {"child": nested}
    deep_frame = {
        **base,
        "unexpected": nested,
    }
    with pytest.raises(WebSocketProtocolError):
        decode_ws_text_frame(json.dumps(deep_frame, separators=(",", ":")))
    assert no_host_execution == []


def test_test012_url_property_gate_never_resolves_or_connects(
    no_host_execution: list[tuple[str, tuple[Any, ...]]],
) -> None:
    corpus = _corpus()
    maximum = _max_generated_input_bytes(corpus)
    rng = random.Random(_campaign_seed(corpus) ^ 0x55524C)

    def forbidden_resolver(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("URL canonicalization attempted DNS")

    def forbidden_socket(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("URL canonicalization attempted a connection")

    policy = OutboundPolicy(
        resolver=forbidden_resolver,
        socket_factory=forbidden_socket,
    )
    for url in corpus["url"]["canonical_valid"]:
        _parsed, target = policy.canonicalize_url(url)
        assert target.canonical_url.startswith(("http://", "https://"))
    for url in corpus["url"]["canonical_invalid"]:
        with pytest.raises(OutboundDenied):
            policy.canonicalize_url(url)

    for case_index in range(_campaign_cases(corpus)):
        label = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16))
        hostile = rng.choice(
            [
                f"file:///{label}",
                f"ftp://{label}.example/file",
                f"https://user:password@{label}.example/",
                f"https://{label}.example\\@127.0.0.1/",
                f"https://{label}.example/%zz",
                f" https://{label}.example/",
                f"https://{label}.example:\u0000/",
            ]
        )
        assert len(hostile.encode("utf-8")) <= maximum
        with pytest.raises(OutboundDenied):
            policy.canonicalize_url(hostile)
    assert no_host_execution == [], f"host operations observed in URL case {case_index}"


def test_test012_file_uri_path_property_gate(
    no_host_execution: list[tuple[str, tuple[Any, ...]]],
) -> None:
    corpus = _corpus()
    maximum = _max_generated_input_bytes(corpus)
    rng = random.Random(_campaign_seed(corpus) ^ 0x50415448)

    valid_file_uris = corpus["path"]["file_uri_valid"]
    if os.name == "nt":
        valid_file_uris = [
            uri.replace("file:///workspace/", "file:///C:/workspace/").replace(
                "file://localhost/workspace/",
                "file://localhost/C:/workspace/",
            )
            for uri in valid_file_uris
        ]
    for uri in valid_file_uris:
        decoded = decode_local_file_uri(uri)
        assert decoded
    for uri in corpus["path"]["file_uri_invalid"]:
        with pytest.raises(ValueError):
            decode_local_file_uri(uri)

    for case_index in range(_campaign_cases(corpus)):
        encoded_separator = rng.choice(["%2f", "%5c"])
        for _ in range(rng.randrange(4)):
            encoded_separator = encoded_separator.replace("%", "%25")
        uri = f"file:///workspace/case-{case_index}{encoded_separator}escape.txt"
        assert len(uri.encode("utf-8")) <= maximum
        with pytest.raises(ValueError):
            decode_local_file_uri(uri)
    assert no_host_execution == []


def test_test012_command_and_classifier_json_fail_closed_without_execution(
    no_host_execution: list[tuple[str, tuple[Any, ...]]],
) -> None:
    corpus = _corpus()
    rng = random.Random(_campaign_seed(corpus) ^ 0x434F4D4D414E44)
    work_dir = ROOT / "test-results" / f"test012-python-{uuid4().hex}"
    work_dir.mkdir(parents=True)

    try:
        malformed_classifications: list[object] = [
            None,
            [],
            {},
            {"verdict": "allow_read_only", "parsed_commands": [], "canonical_digest": "0" * 64},
            {
                "verdict": "allow_read_only",
                "parsed_commands": [["git", "status"]],
                "canonical_digest": "short",
            },
            {
                "verdict": "allow_read_only",
                "parsed_commands": [["git", 7]],
                "canonical_digest": "0" * 64,
            },
        ]
        for value in malformed_classifications:
            parsed = _parse_classification(value, "bash", "git status")
            assert parsed.verdict is ShellVerdict.ASK

        for case_index in range(_campaign_cases(corpus)):
            command = rng.choice(
                corpus["command"]["bash_must_ask"]
                + corpus["command"]["powershell_must_ask"]
            )
            action = normalize_exec_action(
                ["parser-only-shell", "-c", command],
                work_dir,
                raw_command=command,
            )
            assert action.raw_command == command

            malformed = {
                "verdict": "allow_read_only",
                "parsed_commands": [[command]],
                "canonical_digest": "0" * rng.randrange(64),
            }
            assert (
                _parse_classification(malformed, "bash", command).verdict
                is ShellVerdict.ASK
            ), f"malformed classifier case {case_index} was auto-allowed"

        for invalid_argv in ([], [""], ["   "], ["bad\x00token"]):
            with pytest.raises(ValueError):
                normalize_exec_action(invalid_argv, work_dir)

        marker = work_dir / "parser-must-not-execute"
        template = corpus["command"]["host_execution_templates"]["powershell"]
        payload = template.replace("{marker}", str(marker))
        normalize_exec_action(
            ["powershell", "-NoProfile", "-Command", payload],
            work_dir,
            raw_command=payload,
        )
        assert not marker.exists()
        assert no_host_execution == []
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
