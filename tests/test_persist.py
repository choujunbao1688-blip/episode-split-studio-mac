import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from agent import agent


class PersistTests(unittest.TestCase):
    def test_concurrent_saves_leave_valid_state(self):
        original_cache = agent.CACHE
        with tempfile.TemporaryDirectory() as directory:
            agent.CACHE = Path(directory)
            try:
                def save(worker):
                    for iteration in range(50):
                        with agent.LOCK:
                            agent.STATE["settings"] = {"worker": worker, "iteration": iteration}
                        agent.persist()

                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                    list(pool.map(save, range(16)))

                with agent.LOCK:
                    agent.STATE["settings"] = {"complete": True}
                agent.persist()
                state = json.loads((agent.CACHE / "state.json").read_text())
                self.assertEqual(state["settings"], {"complete": True})
                self.assertEqual(list(agent.CACHE.glob("state-*.tmp")), [])
            finally:
                agent.CACHE = original_cache


if __name__ == "__main__":
    unittest.main()
