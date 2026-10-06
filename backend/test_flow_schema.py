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


if __name__ == "__main__":
    unittest.main()
