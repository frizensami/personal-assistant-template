#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Any

import anyio
import mcp_types as types
from anyio import to_thread
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from personal_assistant.hermes_read_client import fetch_read_view

_EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_TOOLS = [
    types.Tool(
        name="personal_assistant_status",
        description="Read Personal Assistant service status and API capabilities.",
        input_schema=_EMPTY_SCHEMA,
        annotations=_ANNOTATIONS,
    ),
    types.Tool(
        name="personal_assistant_active_tasks",
        description="Read active self-tasks from the Personal Assistant service.",
        input_schema=_EMPTY_SCHEMA,
        annotations=_ANNOTATIONS,
    ),
    types.Tool(
        name="personal_assistant_scheduled_reminders",
        description="Read ordinary scheduled reminders from the Personal Assistant service.",
        input_schema=_EMPTY_SCHEMA,
        annotations=_ANNOTATIONS,
    ),
]
_TOOL_VIEWS = {
    "personal_assistant_status": "status",
    "personal_assistant_active_tasks": "tasks",
    "personal_assistant_scheduled_reminders": "reminders",
}


async def list_tools(
    _context: Any,
    _params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_TOOLS)


async def call_tool(
    _context: Any,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    view = _TOOL_VIEWS.get(params.name)
    if view is None:
        return types.CallToolResult(
            content=[types.TextContent(text="Unknown Personal Assistant tool.")],
            is_error=True,
        )
    if params.arguments:
        return types.CallToolResult(
            content=[types.TextContent(text="This read-only tool accepts no arguments.")],
            is_error=True,
        )

    try:
        result = await to_thread.run_sync(fetch_read_view, view)
    except (RuntimeError, ValueError) as error:
        return types.CallToolResult(
            content=[types.TextContent(text=str(error))],
            is_error=True,
        )

    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(result, separators=(",", ":")))],
        structured_content=result,
    )


server = Server(
    "hermes-personal-assistant-read",
    version="0.1.0",
    description="Least-privilege read access to the configured Personal Assistant service.",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
