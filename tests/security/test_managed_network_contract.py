from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_native_network_is_proxy_only_and_has_no_mitm() -> None:
    policy = (ROOT / "security-runtime/src/network/policy.rs").read_text(encoding="utf-8")
    proxy = (ROOT / "security-runtime/src/network/proxy.rs").read_text(encoding="utf-8")
    linux = (ROOT / "security-runtime/src/linux/proxy_routing.rs").read_text(encoding="utf-8")

    assert "cloud metadata endpoints are permanently denied" in policy
    assert "deny wins" in policy or "explicitly denied" in policy
    assert "CONNECT" in proxy
    assert "TcpListener::bind((Ipv4Addr::LOCALHOST" in proxy
    assert "UnixListener::bind" in linux
    assert "rustls" not in proxy.lower()
    assert "certificate" not in proxy.lower()


def test_windows_wfp_uses_stable_account_scoped_filters() -> None:
    source = (ROOT / "security-runtime/src/windows/wfp.rs").read_text(encoding="utf-8")
    assert "FWPM_CONDITION_ALE_USER_ID" in source
    assert "FwpmTransactionBegin0" in source
    assert "FWP_ACTION_PERMIT" in source
    assert "FWP_ACTION_BLOCK" in source
    assert "43119" in source
    assert "session" not in source.split("pub fn install", 1)[1].split("struct Engine", 1)[0].lower()
