import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent import agent


class ManualCutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_cache, self.old_state = agent.CACHE, dict(agent.STATE)
        self.addCleanup(self.restore_state)
        agent.CACHE = self.root / 'cache'
        agent.CACHE.mkdir()
        agent.STATE.clear()
        agent.STATE.update(files=[], plan=[], settings={}, job=None, samples={},
                           sampleCovers={}, tailPreviews={}, warnings=[], orderConfirmed=False)
        self.source_dir = self.root / 'source'
        self.source_dir.mkdir()
        self.first = self.source_dir / '16.mp4'
        self.second = self.source_dir / '17.mp4'
        self.first.write_bytes(b'16: original 125 seconds')
        self.second.write_bytes(b'17: original 80 seconds')
        self.settings = dict(startEpisode=17, target=45, minSeconds=44,
                             maxSeconds=113, removeOld=False,
                             autoRemoveIntroCover=False)

        def fake_probe(path):
            return dict(duration=125 if Path(path).name == '16.mp4' else 80,
                        fps=25, width=1080, height=1920, audio=False)

        self.probe_patch = patch.object(agent, 'probe', side_effect=fake_probe)
        self.analyse_patch = patch.object(agent, 'cached_analyse',
                                          side_effect=lambda f: ([], 0, f['duration']))
        self.probe_patch.start()
        self.analyse_patch.start()
        self.addCleanup(self.probe_patch.stop)
        self.addCleanup(self.analyse_patch.stop)
        agent.scan(str(self.source_dir), '')
        agent.plan(self.settings, lambda *_: None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def restore_state(self):
        agent.CACHE = self.old_cache
        agent.STATE.clear()
        agent.STATE.update(self.old_state)

    def post(self, path, data):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.server.server_port}{path}',
            data=json.dumps(data).encode(),
            headers={'X-Studio-Token': agent.TOKEN,
                     'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_split_single_long_episode_preserves_tail_and_shifts_every_number(self):
        original = hashlib.sha256(self.first.read_bytes()).hexdigest()
        self.assertEqual([r['episode'] for r in agent.STATE['plan']], [17, 18])
        result = self.post('/update-cut', {'index': 0, 'time': 62})
        rows = result['plan']
        self.assertTrue(result['created'])
        self.assertEqual([r['episode'] for r in rows], [17, 18, 19])
        self.assertEqual([(r['source'], r['start'], r['end']) for r in rows],
                         [(0, 0, 62), (0, 62, 125), (1, 0, 80)])
        self.assertEqual([(r['segment'], r['segmentsInSource']) for r in rows[:2]],
                         [(1, 2), (2, 2)])
        self.assertEqual([r['duration'] for r in rows], [62, 63, 80])
        self.assertEqual(hashlib.sha256(self.first.read_bytes()).hexdigest(), original)
        self.assertTrue(self.post('/validate-plan', {'settings': self.settings})['valid'])

        moved = self.post('/update-cut', {'index': 0, 'time': 70})['plan']
        self.assertEqual([(r['start'], r['end']) for r in moved[:2]],
                         [(0, 70), (70, 125)])
        self.assertEqual([r['episode'] for r in moved], [17, 18, 19])

    def test_invalid_cut_reports_chinese_error_without_mutating_plan_or_source(self):
        before = json.dumps(agent.STATE['plan'], sort_keys=True)
        original = self.first.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post('/update-cut', {'index': 0, 'time': 124})
        error = json.loads(caught.exception.read())['error']
        self.assertIn('前后剧情各保留至少 3 秒', error)
        self.assertEqual(json.dumps(agent.STATE['plan'], sort_keys=True), before)
        self.assertEqual(self.first.read_bytes(), original)

    def test_repair_beta5_truncated_tail_before_export(self):
        row = agent.STATE['plan'][0]
        row['end'] = 62
        row['duration'] = 62
        agent.STATE['sourceBounds'] = None  # beta.5 saved no explicit coverage bounds
        agent.persist()
        agent.save_analysis_history()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post('/validate-plan', {'settings': self.settings})
        error = json.loads(caught.exception.read())['error']
        self.assertIn('62.00～125.00 秒', error)
        self.assertIn('已阻止保存和导出', error)
        repaired = self.post('/repair-plan', {})
        self.assertEqual([r['episode'] for r in repaired['plan']], [17, 18, 19])
        self.assertEqual([(r['start'], r['end']) for r in repaired['plan'][:2]],
                         [(0, 62), (62, 125)])
        self.assertTrue(self.post('/validate-plan', {'settings': self.settings})['valid'])
        self.assertEqual(self.first.read_bytes(), b'16: original 125 seconds')

    def test_new_split_cannot_overwrite_old_numbered_delivery(self):
        folder = self.root / 'delivery'
        folder.mkdir()
        video = folder / '17.mp4'
        video.write_bytes(b'old complete 125 seconds')
        settings = dict(self.settings, coverEnabled=False, tailEnabled=False)
        old_signature = agent.render_signature(settings, [agent.STATE['plan'][0]])
        agent.record_export_stamp(agent.export_stamp_path(folder, 17), video, old_signature)
        self.post('/update-cut', {'index': 0, 'time': 62})
        with self.assertRaisesRegex(ValueError, '旧切点或不同的成片设置'):
            agent.export_episodes(agent.STATE['plan'], settings, folder, lambda *_: None)
        self.assertEqual(video.read_bytes(), b'old complete 125 seconds')


if __name__ == '__main__':
    unittest.main()
