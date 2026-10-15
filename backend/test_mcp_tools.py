import unittest

from backend.mcp_tools import McpConnectionError, _plain_tool, expand_mcp_servers, validate_mcp_url
from backend.tool_registry import ToolConfigurationError, validate_attached_tool


class McpToolTests(unittest.TestCase):
    def test_only_https_urls_without_embedded_credentials_are_accepted(self):
        for url in (
            "http://example.com/mcp",
            "https://user:password@example.com/mcp",
            "https://example.com/mcp#fragment",
            "not a URL",
        ):
            with self.subTest(url=url), self.assertRaises(McpConnectionError):
                validate_mcp_url(url)

    def test_local_and_private_targets_are_rejected(self):
        for url in (
            "https://localhost/mcp",
            "https://127.0.0.1/mcp",
            "https://169.254.169.254/latest/meta-data/",
        ):
            with self.subTest(url=url), self.assertRaises(McpConnectionError):
                validate_mcp_url(url)

    def test_mcp_schema_is_exposed_for_model_tool_calling(self):
        tool = {
            "name": "lookup_weather",
            "description": "Get current weather",
            "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
        self.assertEqual(_plain_tool(tool), {
            "name": "lookup_weather",
            "description": "Get current weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        })

    def test_attached_tool_requires_server_and_remote_name(self):
        with self.assertRaises(ToolConfigurationError):
            validate_attached_tool({"tool_id": "mcp", "mcp_tool_name": "weather"}, "answer")
        validate_attached_tool({
            "tool_id": "mcp", "server_url": "https://example.com/mcp",
            "mcp_tool_name": "weather", "input_schema": {"type": "object"},
            "approval_required": True, "parameters": {"operation": "call"},
        }, "answer")

    def test_server_connection_keeps_functions_grouped_until_runtime(self):
        servers = [{
            "tool_id": "mcp", "name": "EverQ Analytics", "server_url": "https://example.com/mcp",
            "approval_required": False,
            "functions": [
                {"name": "get_no_show_analytics", "description": "No-show counts", "input_schema": {"type": "object"}},
                {"name": "get_doctor_performance", "description": "Doctor metrics", "input_schema": {"type": "object"}},
            ],
        }]
        expanded = expand_mcp_servers(servers)
        self.assertEqual(len(servers), 1)
        self.assertEqual([item["mcp_tool_name"] for item in expanded], ["get_no_show_analytics", "get_doctor_performance"])
        self.assertTrue(all(item["mcp_server_name"] == "EverQ Analytics" for item in expanded))
        self.assertTrue(all(item["server_url"] == servers[0]["server_url"] for item in expanded))

    def test_legacy_single_function_attachment_still_runs(self):
        expanded = expand_mcp_servers([{
            "tool_id": "mcp", "name": "get_metrics", "mcp_tool_name": "get_metrics",
            "server_url": "https://example.com/mcp", "input_schema": {"type": "object"},
        }])
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0]["mcp_tool_name"], "get_metrics")


if __name__ == "__main__":
    unittest.main()
