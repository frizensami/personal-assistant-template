from __future__ import annotations

import unittest
from unittest.mock import patch

import anyio
import mcp_types as types

import server


class HermesPersonalAssistantMCPTests(unittest.TestCase):
    def test_exposes_only_closed_read_only_tools(self) -> None:
        self.assertEqual(
            [tool.name for tool in server._TOOLS],
            [
                "personal_assistant_status",
                "personal_assistant_active_tasks",
                "personal_assistant_scheduled_reminders",
            ],
        )
        for tool in server._TOOLS:
            self.assertEqual(
                tool.input_schema,
                {"type": "object", "properties": {}, "additionalProperties": False},
            )
            annotations = tool.annotations
            self.assertIsNotNone(annotations)
            assert annotations is not None
            self.assertTrue(annotations.read_only_hint)
            self.assertFalse(annotations.destructive_hint)
            self.assertTrue(annotations.idempotent_hint)
            self.assertFalse(annotations.open_world_hint)

    def test_rejects_unknown_tools_and_nonempty_arguments(self) -> None:
        async def exercise() -> None:
            unknown = await server.call_tool(
                None,
                types.CallToolRequestParams(name="unknown", arguments={}),
            )
            self.assertTrue(unknown.is_error)

            arguments = await server.call_tool(
                None,
                types.CallToolRequestParams(
                    name="personal_assistant_status",
                    arguments={"path": "/internal/v1/secrets"},
                ),
            )
            self.assertTrue(arguments.is_error)

        anyio.run(exercise)

    def test_valid_tool_returns_the_fixed_view(self) -> None:
        async def exercise() -> None:
            with patch.object(server, "fetch_read_view", return_value={"status": "ok"}) as fetch:
                result = await server.call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="personal_assistant_status",
                        arguments={},
                    ),
                )
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, {"status": "ok"})
            fetch.assert_called_once_with("status")

        anyio.run(exercise)


if __name__ == "__main__":
    unittest.main()
