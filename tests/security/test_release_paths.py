from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_linux_packaged_gateway_uses_the_installed_crew_path() -> None:
    dockerfile = (ROOT / "Dockerfile.pack").read_text(encoding="utf-8")
    main_source = (ROOT / "desktop/src/main/index.ts").read_text(encoding="utf-8")

    # 打包方案已从 PyInstaller 冻结二进制改为内嵌 Python 直跑源码：
    # /opt/crew-gateway 是安装后的源码树，gateway 由
    # runtimes/python/bin/python3 -m crew.gateway.server 启动。
    assert "/opt/crew-gateway" in dockerfile
    assert (
        "/opt/crew-gateway/runtimes/python/bin/python3 -m crew.gateway.server" in dockerfile
    )
    assert "const gatewayRoot = '/opt/crew-gateway';" in main_source


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

    # 安全意图：安装/卸载清理必须按用户作用域（pkill -u "$uid"），
    # 禁止无用户范围的 pkill -f，避免影响其他用户的 gateway/desktop 进程。
    # 打包改为内嵌 Python 后 gateway 进程命令行为
    # runtimes/python/bin/python3 -m crew.gateway.server，按 /opt/crew-gateway 路径匹配。
    assert "pkill -f /opt/crew-gateway" not in dockerfile
    assert "pkill -f '/opt/crew-gateway'" not in dockerfile
    assert "pkill -f /opt/crew-desktop/crew-desktop" not in dockerfile
    assert "pkill -f '/opt/crew-desktop/crew-desktop'" not in dockerfile
    assert "pkill -u \"$uid\" -f '/opt/crew-gateway" in dockerfile
    assert "pkill -u \"$uid\" -f '/opt/crew-desktop/crew-desktop" in dockerfile
