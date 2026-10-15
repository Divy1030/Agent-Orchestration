import unittest

from backend.flow_schema import DEMO_FLOW, FlowValidationError, validate_flow


class FlowSchemaTests(unittest.TestCase):
    def test_demo_flow_is_valid(self):
        self.assertEqual(validate_flow(DEMO_FLOW)["id"], "demo-flow")

    def test_unknown_edge_target_is_rejected(self):
        flow = {**DEMO_FLOW, "edges": [*DEMO_FLOW["edges"], {"from": "welcome", "to": "missing", "key": "bad"}]}
        with self.assertRaises(FlowValidationError):
            validate_flow(flow)

    def test_disconnected_node_is_rejected(self):
        flow = {**DEMO_FLOW, "nodes": [*DEMO_FLOW["nodes"], {"id": "orphan", "type": "message"}]}
        with self.assertRaises(FlowValidationError):
            validate_flow(flow)

    def test_agent_connection_id_must_be_a_string(self):
        flow = {**DEMO_FLOW, "nodes": [{**node, "config": {**node.get("config", {}), "agent_id": 123}} if node["type"] == "agent" else node for node in DEMO_FLOW["nodes"]]}
        with self.assertRaises(FlowValidationError):
            validate_flow(flow)

    def test_tool_node_requires_supported_tool_configuration(self):
        flow = {**DEMO_FLOW,
                "nodes": [*DEMO_FLOW["nodes"], {"id": "mail", "type": "tool", "label": "Unknown Tool", "config": {}}],
                "edges": [*DEMO_FLOW["edges"], {"from": "answer", "to": "mail", "key": "mail"}, {"from": "mail", "to": "finish", "key": "finish"}]}
        with self.assertRaises(FlowValidationError):
            validate_flow(flow)

    def test_tool_node_accepts_catalog_id_and_parameters(self):
        flow = {**DEMO_FLOW,
                "nodes": [*DEMO_FLOW["nodes"], {"id": "http", "type": "tool", "label": "HTTP Request", "config": {"tool_id": "http_request", "parameters": {"method": "GET", "url": "https://example.com"}}}],
                "edges": [*DEMO_FLOW["edges"], {"from": "answer", "to": "http", "key": "http"}, {"from": "http", "to": "finish", "key": "finish"}]}
        self.assertEqual(validate_flow(flow)["id"], "demo-flow")


if __name__ == "__main__":
    unittest.main()
