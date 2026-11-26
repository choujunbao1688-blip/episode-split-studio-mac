import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent import agent, text_clean


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.old_cache, self.old_state = agent.CACHE, dict(agent.STATE)
        agent.CACHE = self.root / 'cache'
        agent.CACHE.mkdir()
        agent.STATE.clear()
        agent.STATE.update(files=[], plan=[], settings={}, job=None, samples={},
                           sampleCovers={}, tailPreviews={}, warnings=[],
                           orderConfirmed=False)
        self.addCleanup(self.restore_state)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        for number in (1, 2):
            (self.sources / f'{number}.mp4').write_bytes(f'video{number}'.encode())
        self.settings = {'target': 3, 'startEpisode': 20, 'minSeconds': 10,
                         'maxSeconds': 30, 'removeOld': False,
                         'autoRemoveIntroCover': False, 'language': 'en'}

    def restore_state(self):
        agent.CACHE = self.old_cache
        agent.STATE.clear()
        agent.STATE.update(self.old_state)

    def create_plan(self):
        with patch.object(agent, 'probe', return_value=dict(duration=50, fps=25,
                                                        width=1080, height=1920,
                                                        audio=True)), patch.object(
                agent, 'cached_analyse', return_value=(
                    [{'time': 25, 'score': 4, 'reason': '剧情', 'confidence': 90}], 0, 50)):
            agent.scan(str(self.sources), '')
            return agent.plan(self.settings, lambda *_: None)

    def test_reimport_restores_manual_cuts_and_language_change_reuses_plan(self):
        result = self.create_plan()
        self.assertEqual([r['episode'] for r in result['plan']], [20, 21, 22, 23])
        rows = agent.STATE['plan']
        rows[0]['end'] = 26
        rows[0]['duration'] = 26
        rows[1]['start'] = 26
        rows[1]['duration'] = 24
        rows[0]['cutReason'] = '人工调整'
        agent.persist()
        agent.save_analysis_history()
        with patch.object(agent, 'probe', return_value=dict(duration=50, fps=25,
                                                        width=1080, height=1920,
                                                        audio=True)):
            restored = agent.scan(str(self.sources), '')
        self.assertTrue(restored['reused'])
        self.assertEqual(restored['plan'][0]['end'], 26)
        changed = dict(self.settings, language='zh')
        with patch.object(agent, 'cached_analyse', side_effect=AssertionError('repeated analysis')):
            again = agent.plan(changed, lambda *_: None)
        self.assertTrue(again['reused'])
        self.assertEqual([r['episode'] for r in again['plan']], [20, 21, 22, 23])
        self.assertEqual(again['plan'][0]['cutReason'], '人工调整')

    def test_source_change_marks_record_unavailable_without_deleting_it(self):
        self.create_plan()
        (self.sources / '1.mp4').write_bytes(b'replaced source')
        self.assertFalse(agent.visible_history()['analysis']['available'])
        with self.assertRaisesRegex(ValueError, '源片或海报已变化'):
            agent.require_current_plan(self.settings)
        self.assertEqual(len(agent.load_history()['analysis']['plan']), 4)

    def test_export_history_links_original_file_and_srt_settings_do_not_reencode(self):
        self.create_plan()
        out = self.root / 'output'
        out.mkdir()
        video = out / '20.mp4'
        video.write_bytes(b'exported')
        row = agent.STATE['plan'][0]
        agent.begin_export_history('normal', out, [row], self.settings)
        agent.record_export_file('normal', 20, video)
        agent.finish_export_history('normal', 'done')
        record = agent.visible_history()['export']['runs']['normal']
        self.assertEqual(record['files'], [{'episode': 20, 'path': str(video), 'available': True}])
        self.assertTrue(record['folderAvailable'])
        baseline = agent.render_signature(self.settings, [row])
        changed = dict(self.settings, exportSrt=True, srtFolder=str(self.root / 'srt'),
                       language='zh', subtitleTopPct=40)
        self.assertEqual(baseline, agent.render_signature(changed, [row]))
        self.assertNotEqual(agent.render_signature(dict(changed, removeText=True), [row]),
                            agent.render_signature(dict(changed, removeText=True,
                                                        subtitleTopPct=60), [row]))
        self.assertNotEqual(text_clean.source_key(video, dict(language='en')),
                            text_clean.source_key(video, dict(language='zh')))
        video.unlink()
        self.assertFalse(agent.visible_history()['export']['runs']['normal']['files'][0]['available'])

    def test_history_restore_and_delete_endpoints_preserve_exported_video(self):
        self.create_plan()
        folder = self.root / 'delivery'
        folder.mkdir()
        video = folder / '20.mp4'
        video.write_bytes(b'keep original output')
        agent.begin_export_history('normal', folder, agent.STATE['plan'], self.settings)
        agent.record_export_file('normal', 20, video)
        server = ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, payload):
            request = urllib.request.Request(
                f'http://127.0.0.1:{server.server_port}{path}',
                data=json.dumps(payload).encode(),
                headers={'X-Studio-Token': agent.TOKEN,
                         'Content-Type': 'application/json'},
                method='POST')
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.load(response)

        agent.STATE.update(plan=[], files=[], planParams=None)
        restored = post('/history/restore', {'kind': 'export'})
        self.assertEqual(restored['plan'][0]['episode'], 20)
        self.assertEqual(restored['outputFolder'], str(folder))
        deleted = post('/history/delete', {'kind': 'export'})
        self.assertIsNone(deleted['export'])
        self.assertEqual(video.read_bytes(), b'keep original output')


if __name__ == '__main__':
    unittest.main()
