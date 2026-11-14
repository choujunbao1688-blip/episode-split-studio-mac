import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from PIL import Image
from agent import agent


class PosterTests(unittest.TestCase):
    def test_select_poster_after_import_without_rescanning_videos(self):
        original_cache = agent.CACHE
        original_poster = agent.STATE.get("poster")
        with tempfile.TemporaryDirectory() as directory:
            agent.CACHE = Path(directory)
            poster = agent.CACHE / "clean.png"
            Image.new("RGB", (9, 16), "#bf904a").save(poster)
            server = agent.ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                data = json.dumps({"poster": str(poster)}).encode()
                request = urllib.request.Request(
                    base + "/set-poster",
                    data=data,
                    headers={"X-Studio-Token": agent.TOKEN, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)["poster"], str(poster))
                request = urllib.request.Request(
                    base + "/poster", headers={"X-Studio-Token": agent.TOKEN}
                )
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/png")
                    self.assertEqual(response.read(), poster.read_bytes())
                self.assertEqual(
                    json.loads((agent.CACHE / "state.json").read_text())["poster"],
                    str(poster),
                )
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
                agent.CACHE = original_cache
                with agent.LOCK:
                    agent.STATE["poster"] = original_poster


if __name__ == "__main__":
    unittest.main()
