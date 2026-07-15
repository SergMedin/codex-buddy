import importlib.util
import os
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"


def load_script(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_path_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


start_bridge = load_script("start_bridge")
hook_entry = load_script("hook_entry")


class CodexHomePathTest(unittest.TestCase):
    def test_environment_is_the_single_codex_home_source(self):
        expected = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()

        self.assertEqual(start_bridge.DEFAULT_CODEX_HOME, expected)
        self.assertEqual(hook_entry.DEFAULT_CODEX_HOME, expected)
        self.assertNotIn("codex_home", start_bridge.DEFAULT_CONFIG)
        self.assertEqual(start_bridge.bridge_env()["CODEX_HOME"], str(expected))

        command = start_bridge.bridge_command(start_bridge.DEFAULT_CONFIG)
        codex_home_arg = command.index("--codex-home") + 1
        self.assertEqual(command[codex_home_arg], str(expected))
        self.assertEqual(hook_entry.APPROVAL_SOCK_PATH, expected / "codex-usage-bridge" / "approval.sock")


if __name__ == "__main__":
    unittest.main()
