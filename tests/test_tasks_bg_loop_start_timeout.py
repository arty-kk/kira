import importlib.util
import pathlib
import unittest
from unittest.mock import patch


class _FakeLoop:
    def __init__(self):
        self._is_running = False
        self.closed = False
        self.exception_handler = None

    def is_running(self):
        return self._is_running

    def set_exception_handler(self, handler):
        self.exception_handler = handler

    def call_soon_threadsafe(self, callback):
        callback()

    def stop(self):
        self._is_running = False

    def run_forever(self):
        self._is_running = True

    def close(self):
        self.closed = True


class _FakeEvent:
    def __init__(self):
        self._is_set = False
        self.wait_calls = []

    def set(self):
        self._is_set = True

    def clear(self):
        self._is_set = False

    def is_set(self):
        return self._is_set

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return False


class _FakeThread:
    def __init__(self, target, daemon, name):
        self._target = target
        self.daemon = daemon
        self.name = name

    def start(self):
        return None

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class BgLoopStartTimeoutTests(unittest.TestCase):
    def test_get_bg_loop_raises_and_resets_state_when_start_timeout(self):
        module_name = "bg_loop_start_timeout_under_test"
        module_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "utils" / "bg_loop.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)

        created_loops = []

        def _new_event_loop():
            loop = _FakeLoop()
            created_loops.append(loop)
            return loop

        with (
            patch("threading.Event", _FakeEvent),
            patch("threading.Thread", _FakeThread),
            patch("asyncio.new_event_loop", side_effect=_new_event_loop),
        ):
            spec.loader.exec_module(module)
            with self.assertRaises(RuntimeError):
                module.get_bg_loop()

        self.assertEqual(len(created_loops), module._BG_START_MAX_ATTEMPTS)
        self.assertTrue(all(loop.closed for loop in created_loops))
        self.assertIsNone(module._BG_LOOP)
        self.assertIsNone(module._BG_THREAD)
        self.assertFalse(module._BG_STARTED.is_set())
        self.assertEqual(module._BG_STARTED.wait_calls, [module._BG_START_TIMEOUT_SEC] * module._BG_START_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
