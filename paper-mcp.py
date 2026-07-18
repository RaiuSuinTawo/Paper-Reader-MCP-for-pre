"""Paper-Assistant-MCP 入口(基于 MinerU 云端 API)。

运行:  python paper-mcp.py   (stdio 传输,通常由 MCP 客户端拉起)
实现分层在同目录的 paper_mcp/ 包内:
  config / mineru / cache / content / parsing / output / server
"""
from paper_mcp.server import mcp

if __name__ == "__main__":
    mcp.run()
