import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.tools_agents as agents
from mcp_server.security import load_settings


class AgentOrchestrationTests(unittest.TestCase):
    def test_wait_modes(self):
        self.assertFalse(agents._wait_condition(1, 3, "all"))
        self.assertTrue(agents._wait_condition(1, 3, "any"))
        self.assertFalse(agents._wait_condition(1, 3, "majority"))
        self.assertTrue(agents._wait_condition(2, 3, "majority"))

    def test_opencode_progress_event_tracks_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent_id = "agt_testprogress"
            adir = root / agent_id
            adir.mkdir()
            meta = {
                "agent_id": agent_id, "status": "running", "phase": "provider_starting",
                "started_at": 1.0, "spawn_requested_at": 1.0, "last_activity_at": 1.0,
                "step_count": 0, "tool_call_count": 0,
            }
            (adir / "meta.json").write_text(json.dumps(meta))
            event = json.dumps({"type":"tool_use","part":{"tool":"bash","time":{"start":100,"end":145}}})
            with patch.object(agents, "AGENTS_DIR", root):
                agents._record_provider_event(agent_id, event)
                saved = agents._read_meta(agent_id)
            self.assertEqual("tool", saved["phase"])
            self.assertEqual(1, saved["tool_call_count"])
            self.assertEqual("bash", saved["last_tool"])
            self.assertEqual(45, saved["last_tool_duration_ms"])
            self.assertIsNotNone(saved["first_event_at"])
            self.assertIsNotNone(saved["first_tool_at"])

    def test_team_children_cannot_override_shared_model(self):
        with self.assertRaises(HTTPException) as ctx:
            agents.spawn_agents(
                load_settings(), tasks=[{"prompt":"x", "model":"different"}],
                provider="opencode", model="opencode/muse-spark-1.2-contributor-free",
            )
        self.assertEqual(400, ctx.exception.status_code)

    def test_public_meta_has_progress_fields(self):
        meta = {
            "status":"running", "started_at":10.0, "spawn_requested_at":10.0,
            "last_activity_at":10.5, "first_event_at":10.25, "step_count":2,
            "tool_call_count":3, "retries":1, "retry_count":0,
        }
        with patch.object(agents, "_now", return_value=11.0):
            out = agents._public_meta("agt_x", meta)
        self.assertEqual(250, out["first_event_latency_ms"])
        self.assertEqual(0.5, out["idle_seconds"])
        self.assertEqual(2, out["step_count"])
        self.assertEqual(3, out["tool_call_count"])


if __name__ == "__main__":
    unittest.main()
