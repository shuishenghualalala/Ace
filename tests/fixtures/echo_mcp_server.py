"""最小 stdio MCP server，仅供 test_mcp.py 测 MCP Client 连接用。

暴露一个 echo 工具。运行：python echo_mcp_server.py（由测试以 stdio 子进程拉起）。
"""

from mcp.server import MCPServer

mcp = MCPServer("echo", version="2.0-test")


@mcp.tool()
def echo(text: str) -> str:
    """原样回显输入。"""
    return f"echo: {text}"


@mcp.tool()
def fail(message: str) -> str:
    """返回一个标准 MCP 工具错误。"""
    raise RuntimeError(message)


if __name__ == "__main__":
    mcp.run()
