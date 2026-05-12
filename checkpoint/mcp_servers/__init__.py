"""MCP server wrappers around the REST twins (Phase 6).

Each module here exposes one twin's full Archal tool surface as MCP tools
via FastMCP, mounted on the same FastAPI app the REST twin runs on. One
twin process, two transports (REST + MCP), one `STATE` dict.
"""
