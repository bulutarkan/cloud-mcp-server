import os
import unittest
from unittest.mock import patch

from mcp_server import tools_desktop as td


class Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DesktopToolTests(unittest.TestCase):
    def test_parse_windows(self):
        raw = (
            "0x03200004  0 3433610 0 48 1680 985 chromium "
            "Persisted:2 - Chromium\n"
        )
        windows = td._parse_windows(raw)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["window_id"], "0x03200004")
        self.assertEqual(windows[0]["pid"], 3433610)
        self.assertEqual(windows[0]["width"], 1680)
        self.assertEqual(windows[0]["title"], "Persisted:2 - Chromium")

    def test_click_does_not_use_xdotool_sync(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            return Proc()
        with patch.object(td, "_run", side_effect=fake_run):
            result = td.desktop_click(100, 200, button="left")
        self.assertTrue(result["ok"])
        self.assertNotIn("--sync", calls[0])
        self.assertEqual(calls[0][-1], "1")

    def test_unicode_type_uses_clipboard_and_paste(self):
        with patch.object(td, "_clipboard_read", return_value=(True, b"old")), \
             patch.object(td, "_clipboard_write", return_value=True) as write, \
             patch.object(td, "_run", return_value=Proc()) as run:
            result = td.desktop_type("Türkiye şçöğü", clear=True)
        self.assertTrue(result["ok"])
        self.assertEqual(write.call_args_list[0].args[0], "Türkiye şçöğü".encode("utf-8"))
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+a"], commands)
        self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+v"], commands)

    def test_profile_env_override(self):
        with patch.dict(os.environ, {"CLOUD_MCP_CHROMIUM_PROFILE": "/tmp/example-profile"}, clear=False):
            self.assertEqual(str(td._chromium_profile()), "/tmp/example-profile")

    def test_chromium_windows_only_returns_owned_profile(self):
        windows = [
            {"window_id": "0x1", "pid": 101, "wm_class": "chromium", "title": "ours"},
            {"window_id": "0x2", "pid": 202, "wm_class": "chromium", "title": "other"},
        ]
        with patch.dict(os.environ, {"CLOUD_MCP_CHROMIUM_PROFILE": "/home/u/ours"}, clear=False), \
             patch.object(td, "desktop_windows", return_value={"ok": True, "windows": windows}), \
             patch.object(td, "_pid_command_line", side_effect=lambda pid: (
                 "/snap/chromium/chrome --user-data-dir=/home/u/ours" if pid == 101
                 else "/snap/chromium/chrome --user-data-dir=/home/u/other"
             )):
            owned = td._chromium_windows()
        self.assertEqual([w["window_id"] for w in owned], ["0x1"])

    def test_open_url_uses_real_gui_address_bar(self):
        window = {"window_id": "0x1", "wm_class": "chromium", "title": "Chromium"}
        with patch.object(td, "_chromium_windows", return_value=[window]), \
             patch.object(td, "desktop_focus", return_value={"ok": True}), \
             patch.object(td, "desktop_key", return_value={"ok": True}) as key, \
             patch.object(td, "desktop_type", return_value={"ok": True}) as type_text:
            result = td.chromium_open_url("https://example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "real_gui_address_bar")
        self.assertEqual(key.call_args_list[0].args[0], "ctrl+l")
        self.assertEqual(key.call_args_list[-1].args[0], "Return")
        type_text.assert_called_once_with("https://example.com", clear=False)

    def test_desktop_act_batches_actions(self):
        with patch.object(td, "desktop_click", return_value={"ok": True}) as click, \
             patch.object(td, "desktop_type", return_value={"ok": True}) as type_text, \
             patch.object(td, "desktop_observe", return_value="STATE"):
            result = td.desktop_act([
                {"type": "click", "x": 1, "y": 2},
                {"type": "type", "text": "hello"},
            ], return_state=True, include_screenshot=False)
        self.assertIsInstance(result, list)
        self.assertEqual(result[-1], "STATE")
        click.assert_called_once()
        type_text.assert_called_once()

    def test_desktop_act_stops_on_error(self):
        with patch.object(td, "desktop_click", return_value={"ok": False, "error": "nope"}), \
             patch.object(td, "desktop_type", return_value={"ok": True}) as type_text:
            result = td.desktop_act([
                {"type": "click", "x": 1, "y": 2},
                {"type": "type", "text": "should-not-run"},
            ], return_state=False, stop_on_error=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actions_completed"], 1)
        type_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
