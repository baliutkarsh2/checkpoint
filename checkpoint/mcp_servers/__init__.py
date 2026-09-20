"""MCP server wrappers around the REST twins.

Each module here exposes one twin's full tool surface as MCP tools
via FastMCP, mounted on the same FastAPI app the REST twin runs on. One
twin process, two transports (REST + MCP), one `STATE` dict.
"""
