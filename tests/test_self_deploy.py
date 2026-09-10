import tempfile
import unittest
from pathlib import Path

from mcp_server import self_deploy_worker as worker
from mcp_server import tools_update


class SelfDeployTests(unittest.TestCase):
    def test_copy_and_rollback_managed_files(self):
        with tempfile.TemporaryDirectory(prefix="cloud-mcp-deploy-test-") as temp:
            base = Path(temp)
            repo = base / "repo"
            runtime = base / "runtime"
            backup = base / "backup"
            (repo / "mcp_server").mkdir(parents=True)
            (runtime / "mcp_server").mkdir(parents=True)
            (repo / "mcp_server" / "vendor").mkdir(parents=True)
            (repo / "mcp_server" / "main.py").write_text("new-main", encoding="utf-8")
            (repo / "mcp_server" / "new_module.py").write_text("new-module", encoding="utf-8")
            (repo / "mcp_server" / "requirements.txt").write_text("x==1\n", encoding="utf-8")
            (repo / "mcp_server" / "vendor" / "asset.js").write_text("new-vendor", encoding="utf-8")
            (runtime / "mcp_server" / "main.py").write_text("old-main", encoding="utf-8")
            (runtime / "mcp_server" / "requirements.txt").write_text("old==1\n", encoding="utf-8")
            manifest = worker.copy_managed(repo, runtime, backup)
            self.assertEqual("new-main", (runtime / "mcp_server" / "main.py").read_text())
            self.assertTrue((runtime / "mcp_server" / "new_module.py").exists())
            self.assertEqual("new-vendor", (runtime / "mcp_server" / "vendor" / "asset.js").read_text())
            self.assertIn(Path("mcp_server/vendor/asset.js"), tools_update._managed_relpaths(repo))
            worker.rollback(runtime, backup, manifest)
            self.assertEqual("old-main", (runtime / "mcp_server" / "main.py").read_text())
            self.assertFalse((runtime / "mcp_server" / "new_module.py").exists())
            self.assertFalse((runtime / "mcp_server" / "vendor" / "asset.js").exists())

    def test_safe_deployment_id_validation(self):
        self.assertTrue(tools_update.re_safe_id("20260907-120000-abcd1234"))
        self.assertFalse(tools_update.re_safe_id("../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
