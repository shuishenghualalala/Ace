"""手工拉起仿真站点：python -m tests.fixtures.mocksites --port 8799"""

from __future__ import annotations

import argparse

from .server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Crew 录制功能本地仿真站点")
    parser.add_argument("--port", type=int, default=8799, help="监听端口（0 = 系统分配）")
    args = parser.parse_args()

    server, _state = build_server(args.port)
    host, port = server.server_address[0], server.server_address[1]
    print(f"仿真站点已启动： http://{host}:{port}/")
    print(f"  站点 A · 内网工单  http://{host}:{port}/ticket/list")
    print(f"  站点 B · 内容站    http://{host}:{port}/feed/")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
