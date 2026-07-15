import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
PLUGIN_BRIDGE = REPO_ROOT / "plugins" / "codex-usage-stick" / "scripts" / "codex_usage_ble_bridge.py"
STANDALONE_BRIDGE = REPO_ROOT / "tools" / "codex_usage_ble_bridge.py"


class BridgeCopySyncTest(unittest.TestCase):
    def test_plugin_and_standalone_bridge_are_identical(self):
        self.assertEqual(PLUGIN_BRIDGE.read_bytes(), STANDALONE_BRIDGE.read_bytes())


if __name__ == "__main__":
    unittest.main()
