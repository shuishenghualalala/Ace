from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_linux_packaged_gateway_uses_the_installed_crew_path() -> None:
    dockerfile = (ROOT / "Dockerfile.pack").read_text(encoding="utf-8")
    main_source = (ROOT / "desktop/src/main/index.ts").read_text(encoding="utf-8")

    assert "/opt/crew-gateway/crew-gateway" in dockerfile
    assert "const gatewayExePath = '/opt/crew-gateway/crew-gateway';" in main_source


def test_packaged_dist_commands_verify_the_runtime_on_every_desktop_platform() -> None:
    package_json = (ROOT / "desktop/package.json").read_text(encoding="utf-8")

    assert '"dist:linux": "npm run security:verify &&' in package_json
    assert '"dist:win": "npm run security:verify &&' in package_json
    assert '"dist:mac": "npm run security:verify &&' in package_json


def test_desktop_and_backend_share_the_public_crew_home_default() -> None:
    session_file = (ROOT / "desktop/src/main/crew-session-file.ts").read_text(encoding="utf-8")
    home_module = (ROOT / "crew/state/home.py").read_text(encoding="utf-8")

    assert "const DEFAULT_HOME_DIRNAME = '.Crew';" in session_file
    assert 'DEFAULT_HOME_DIRNAME = ".Crew"' in home_module


def test_package_scripts_scope_process_cleanup_per_user() -> None:
    dockerfile = (ROOT / "Dockerfile.pack").read_text(encoding="utf-8")

    assert "pkill -f /opt/crew-gateway/crew-gateway" not in dockerfile
    assert "pkill -f /opt/crew-desktop/crew-desktop" not in dockerfile
    assert "pkill -u \"$uid\" -f '/opt/crew-gateway/crew-gateway" in dockerfile
    assert "pkill -u \"$uid\" -f '/opt/crew-desktop/crew-desktop" in dockerfile
