# catalog-mcp-server

An MCP server that exposes a competitive product intelligence catalog to an LLM client as four typed, read-only tools.

The server lives in [`catalog-mcp-server/`](./catalog-mcp-server) — see its
[README](./catalog-mcp-server/README.md) for the architecture, the tool reference, design
notes, and setup instructions.

```
cd catalog-mcp-server
uv sync
uv run python -m catalog.seed
uv run pytest -q
```

## Origin

Started from the Python weather server in
[modelcontextprotocol/quickstart-resources](https://github.com/modelcontextprotocol/quickstart-resources).
The quickstart was scaffolding: its FastMCP conventions and stdio entry point carried over,
its weather domain did not. The other language examples from that repository have been
removed.
