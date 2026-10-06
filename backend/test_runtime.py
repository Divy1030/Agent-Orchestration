import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.flow_schema import DEMO_FLOW
from backend.runtime import FlowRuntime, SessionStore


async def first_route(flow, node_id, message, history):
    edge = next(edge for edge in flow["edges"] if edge["from"] == node_id)
    return {"choice": edge["key"], "target": edge["to"], "provider": "test"}


class RuntimeTests(unittest.TestCase):
    def test_session_turn_persists_state_and_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = FlowRuntime(SessionStore(str(Path(directory) / "runtime.db")))
            session = asyncio.run(runtime.start(DEMO_FLOW))
            result = asyncio.run(runtime.turn(DEMO_FLOW, session["id"], "hello", first_route))
            saved = runtime.store.get(session["id"])
            self.assertEqual(result["current_node"], "answer")
            self.assertEqual(saved["current_node"], "answer")
            self.assertEqual(len(saved["trace"]), 2)

    def test_workflow_agent_is_passed_to_agent_nodes(self):
        seen: list[str] = []

        async def execute_agent(node, messages):
            seen.append(node["config"]["agent_id"])
            return {"reply": "workflow agent reply", "provider": "test"}

        with tempfile.TemporaryDirectory() as directory:
            runtime = FlowRuntime(SessionStore(str(Path(directory) / "runtime.db")), agent_executor=execute_agent)
            flow = {**DEMO_FLOW, "agent_id": "agent-123"}
            session = asyncio.run(runtime.start(flow))
            result = asyncio.run(runtime.turn(flow, session["id"], "hello", first_route))
            self.assertEqual(result["reply"], "workflow agent reply")
            self.assertEqual(seen, ["agent-123"])


if __name__ == "__main__":
    unittest.main()
