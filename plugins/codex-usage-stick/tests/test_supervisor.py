import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "start_bridge.py"
SPEC = importlib.util.spec_from_file_location("start_bridge_supervisor_test", SCRIPT)
assert SPEC and SPEC.loader
start_bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = start_bridge
SPEC.loader.exec_module(start_bridge)


class StubbornProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired("bridge", timeout)
        return -9


class SupervisorTest(unittest.TestCase):
    def test_heartbeat_timeout_covers_update_interval(self):
        cfg = dict(start_bridge.DEFAULT_CONFIG)
        cfg.update(interval=120, update_timeout=20, heartbeat_timeout=90)
        self.assertEqual(start_bridge.effective_heartbeat_timeout(cfg), 145)

    def test_nonpositive_heartbeat_timeout_disables_watchdog(self):
        cfg = dict(start_bridge.DEFAULT_CONFIG)
        cfg["heartbeat_timeout"] = 0
        self.assertEqual(start_bridge.effective_heartbeat_timeout(cfg), 0)

    def test_invalid_timeout_uses_safe_default(self):
        cfg = dict(start_bridge.DEFAULT_CONFIG)
        cfg["heartbeat_timeout"] = "invalid"
        self.assertEqual(start_bridge.effective_heartbeat_timeout(cfg), 90)

    def test_killed_process_is_reaped(self):
        proc = StubbornProcess()
        start_bridge.terminate_process(proc, timeout=0.1)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertEqual(proc.wait_timeouts, [0.1, None])


if __name__ == "__main__":
    unittest.main()
