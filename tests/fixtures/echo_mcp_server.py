"""最小 stdio MCP server，仅供 test_mcp.py 测 MCP Client 连接用。

暴露一个 echo 工具。运行：python echo_mcp_server.py（由测试以 stdio 子进程拉起）。
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """原样回显输入。"""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()
