import json
import os
import unittest
import threading
from unittest.mock import patch

from mcp_server import tools_browser_semantic as bs


class BrowserSemanticTests(unittest.TestCase):
    def test_debug_port_env(self):
        with patch.dict(os.environ, {"CLOUD_MCP_CHROMIUM_DEBUG_PORT": "9333"}, clear=False):
            self.assertEqual(bs._debug_port(), 9333)
            self.assertEqual(bs._base_url(), "http://127.0.0.1:9333")

    def test_list_tabs_filters_non_pages(self):
        payload = [
            {"type": "background_page", "id": "bg", "url": "chrome-extension://x", "title": "BG"},
            {"type": "page", "id": "p1", "url": "https://example.com", "title": "Example", "webSocketDebuggerUrl": "ws://x"},
        ]
        with patch.object(bs, "_json_get", return_value=payload):
            result = bs.browser_list_tabs()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["tabs"][0]["tab_id"], "p1")

    def test_observe_returns_semantic_fields(self):
        observed = {
            "ok": True,
            "observation_id": "bobs_test",
            "dom_revision": 1,
            "url": "https://example.com",
            "title": "Example",
            "scroll": {"x": 0, "y": 0},
            "viewport": {"w": 800, "h": 600},
            "scope": "interactive",
            "element_count": 1,
            "elements": [{"element_id": "e1", "tag": "input", "role": "textbox", "placeholder": "Email", "actionable": True}],
        }
        target = {"id": "tab1", "webSocketDebuggerUrl": "ws://x"}
        with patch.object(bs, "_target", return_value=target), \
             patch.object(bs, "_evaluate", return_value=json.dumps(observed)), \
             patch.object(bs, "_capture", return_value=(None, {})):
            result = bs.browser_observe()
        self.assertEqual(result["tab_id"], "tab1")
        self.assertEqual(result["elements"][0]["role"], "textbox")
        self.assertEqual(result["elements"][0]["element_id"], "e1")

    def test_browser_do_open_wait_extract_close(self):
        with patch.object(bs, "browser_open_url", return_value={"ok": True, "tab_id": "tab-new", "url": "https://example.com", "new_tab": True, "background": True}), \
             patch.object(bs, "_target", return_value={"id": "tab-new", "webSocketDebuggerUrl": "ws://x"}), \
             patch.object(bs, "_wait_browser", return_value={"ok": True, "type": "wait", "for": "network_idle", "matched": True}), \
             patch.object(bs, "_extract_browser", return_value={"ok": True, "type": "extract", "url": "https://example.com", "title": "Example", "data": {"price": ["£99"]}}), \
             patch.object(bs, "browser_close_tab", return_value={"ok": True, "closed": True}):
            result = bs.browser_do(url="https://example.com", extract=["price"], close_after=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["price"], ["£99"])
        self.assertTrue(result["closed"])
        self.assertEqual(result["tab_id"], "tab-new")
        self.assertEqual(result["url"], "https://example.com")
        self.assertEqual(result["title"], "Example")

    def test_browser_do_runs_actions_sequentially(self):
        calls = []
        def fake_act(actions, **kwargs):
            calls.append(actions[0])
            return {"ok": True, "actions": [{"ok": True, "type": actions[0]["type"], "element_id": actions[0].get("element_id")}], "url": "https://example.com", "title": "Example"}
        with patch.object(bs, "_target", return_value={"id": "tab-1"}), \
             patch.object(bs, "browser_act", side_effect=fake_act):
            result = bs.browser_do(actions=[{"type": "click", "element_id": "e1"}, {"type": "type", "element_id": "e2", "text": "hello"}])
        self.assertTrue(result["ok"])
        self.assertEqual([c["type"] for c in calls], ["click", "type"])

    def test_browser_do_rejects_close_existing_tab(self):
        with patch.object(bs, "_target", return_value={"id": "tab-1"}):
            with self.assertRaises(bs.HTTPException):
                bs.browser_do(tab_id="tab-1", actions=[{"type": "wait", "for": "dom_stable", "required": False, "timeout_s": 0.1}], close_after=True)

    def test_find_prefers_exact_match(self):
        observed = {
            "tab_id": "tab1",
            "observation_id": "obs1",
            "elements": [
                {"element_id": "e1", "text": "Save form later", "role": "button", "tag": "button", "actionable": True},
                {"element_id": "e2", "text": "Save form", "role": "button", "tag": "button", "actionable": True},
            ],
        }
        with patch.object(bs, "_target", return_value={"id": "tab1", "webSocketDebuggerUrl": "ws://x"}), \
             patch.object(bs, "browser_observe", return_value=observed):
            result = bs.browser_find("Save form", actionable_only=True)
        self.assertEqual(result["best_match"]["element_id"], "e2")

    def test_act_uses_observation_and_element_id(self):
        target = {"id": "tab1", "webSocketDebuggerUrl": "ws://x"}
        action_result = {"ok": True, "actions": [{"index": 0, "type": "click", "element_id": "e1", "ok": True}]}
        with patch.object(bs, "_target", return_value=target), \
             patch.object(bs, "_resolve_actions", return_value=([{"type": "click", "element_id": "e1"}], None)), \
             patch.object(bs, "_evaluate", return_value=json.dumps(action_result)) as evaluate:
            result = bs.browser_act([{"type": "click", "element_id": "e1"}], observation_id="obs1", return_state="none")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tab_id"], "tab1")
        self.assertIn("obs1", evaluate.call_args.args[1])

    def test_new_tab_defaults_to_background_without_activation(self):
        with patch.object(bs, "_browser_target", return_value={"webSocketDebuggerUrl": "ws://browser"}), \
             patch.object(bs, "_cdp_call", return_value={"targetId": "newtab"}) as call, \
             patch.object(bs, "browser_activate_tab", return_value={"ok": True}) as activate:
            result = bs.browser_open_url("https://example.com", new_tab=True)
        self.assertEqual(result["tab_id"], "newtab")
        self.assertTrue(result["new_tab"])
        self.assertTrue(result["background"])
        self.assertFalse(result["foreground_forced"])
        self.assertEqual(call.call_args.args[1], "Target.createTarget")
        self.assertTrue(call.call_args.args[2]["background"])
        activate.assert_not_called()

    def test_new_tab_can_explicitly_activate(self):
        with patch.object(bs, "_browser_target", return_value={"webSocketDebuggerUrl": "ws://browser"}), \
             patch.object(bs, "_cdp_call", return_value={"targetId": "newtab"}), \
             patch.object(bs, "browser_activate_tab", return_value={"ok": True}) as activate:
            result = bs.browser_open_url("https://example.com", new_tab=True, background=False)
        self.assertFalse(result["background"])
        self.assertTrue(result["foreground_forced"])
        activate.assert_called_once_with("newtab")

    def test_activate_alias_preserves_backward_compatibility(self):
        with patch.object(bs, "_browser_target", return_value={"webSocketDebuggerUrl": "ws://browser"}), \
             patch.object(bs, "_cdp_call", return_value={"targetId": "newtab"}), \
             patch.object(bs, "browser_activate_tab", return_value={"ok": True}) as activate:
            result = bs.browser_open_url("https://example.com", new_tab=True, activate=True)
        self.assertFalse(result["background"])
        activate.assert_called_once_with("newtab")

    def test_close_tab_calls_target_close(self):
        target = {"id": "tab1", "webSocketDebuggerUrl": "ws://x"}
        with patch.object(bs, "_target", return_value=target), \
             patch.object(bs, "_browser_target", return_value={"webSocketDebuggerUrl": "ws://browser"}), \
             patch.object(bs, "_cdp_call", return_value={"success": True}) as call:
            result = bs.browser_close_tab("tab1")
        self.assertTrue(result["closed"])
        self.assertEqual(call.call_args.args[1], "Target.closeTarget")

    def test_same_tab_lease_fails_fast_and_recovers(self):
        target = {"id": "tab1", "webSocketDebuggerUrl": "ws://x"}
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def holder():
            with bs._tab_lease("tab1"):
                entered.set()
                release.wait(timeout=2)

        with patch.object(bs, "_target", return_value=target):
            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(timeout=1))
            try:
                with bs._tab_lease("tab1"):
                    pass
            except bs.HTTPException as exc:
                errors.append(exc)
            self.assertEqual(errors[0].status_code, 409)
            self.assertEqual(errors[0].detail["error"], "tab_busy")
            self.assertTrue(errors[0].detail["retryable"])
            release.set()
            thread.join(timeout=2)
            with bs._tab_lease("tab1"):
                pass

    def test_different_tabs_can_be_leased_in_parallel(self):
        def fake_target(tab_id=None):
            return {"id": str(tab_id or "tab1"), "webSocketDebuggerUrl": "ws://x"}
        with patch.object(bs, "_target", side_effect=fake_target):
            with bs._tab_lease("tab1"):
                with bs._tab_lease("tab2") as second:
                    self.assertEqual(second["id"], "tab2")

    def test_list_tabs_marks_busy_tab(self):
        payload = [{"type": "page", "id": "p1", "url": "https://example.com", "title": "Example", "webSocketDebuggerUrl": "ws://x"}]
        with patch.object(bs, "_json_get", return_value=payload), patch.object(bs, "_target", return_value=payload[0]):
            with bs._tab_lease("p1"):
                result = bs.browser_list_tabs()
                self.assertTrue(result["tabs"][0]["busy"])
            result = bs.browser_list_tabs()
            self.assertFalse(result["tabs"][0]["busy"])


if __name__ == "__main__":
    unittest.main()
