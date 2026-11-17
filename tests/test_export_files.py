import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import agent


class ExportFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'source footage')
        self.output = self.root / 'deliver'
        self.output.mkdir()
        self.rows = [
            {'episode': 20, 'source': 0, 'start': 0, 'end': 45, 'duration': 45},
            {'episode': 21, 'source': 0, 'start': 45, 'end': 90, 'duration': 45},
        ]
        self.old_cache, self.old_state = agent.CACHE, dict(agent.STATE)
        agent.CACHE = self.root / 'app-cache'
        agent.STATE.update(files=[{'path': str(self.source)}], poster='')
        self.addCleanup(self.restore)

    def restore(self):
        agent.CACHE = self.old_cache
        agent.STATE.clear()
        agent.STATE.update(self.old_state)

    def test_numbered_mp4_only_and_resumes_without_sidecars(self):
        rendered = []

        def render(row, settings, dest):
            rendered.append(row['episode'])
            dest.write_bytes(f"video-{row['episode']}-{settings['quality']}".encode())
            return str(dest)

        with patch.object(agent, 'export_one', side_effect=render), patch.object(agent, 'probe', return_value={'duration': 46}):
            result = agent.export_episodes(self.rows, {'quality': 'a'}, self.output, lambda *args: None)
            self.assertEqual(result['files'], 2)
            self.assertEqual(sorted(p.name for p in self.output.iterdir()), ['20.mp4', '21.mp4'])
            agent.export_episodes(self.rows, {'quality': 'a'}, self.output, lambda *args: None)
            self.assertEqual(rendered, [20, 21])
            agent.export_episodes(self.rows, {'quality': 'b'}, self.output, lambda *args: None)
            self.assertEqual(rendered, [20, 21, 20, 21])
            self.assertEqual(sorted(p.name for p in self.output.iterdir()), ['20.mp4', '21.mp4'])
            self.assertTrue(agent.export_stamp_path(self.output, 20).is_file())
            self.assertFalse(agent.export_stamp_path(self.output, 20).is_relative_to(self.output))

    def test_existing_unowned_numeric_video_is_never_overwritten(self):
        existing = self.output / '21.mp4'
        existing.write_bytes(b'keep me')
        with patch.object(agent, 'export_one') as render:
            with self.assertRaisesRegex(ValueError, '21.mp4 已存在'):
                agent.export_episodes(self.rows, {}, self.output, lambda *args: None)
            render.assert_not_called()
        self.assertEqual(existing.read_bytes(), b'keep me')
        self.assertFalse((self.output / '20.mp4').exists())

    def test_srt_folder_is_separate_and_matches_episode_numbers(self):
        from unittest.mock import patch
        captions = self.root / 'captions'
        events = [{'kind':'subtitle','start':1,'end':2,'text':'Hello there'}]

        def render(row, settings, dest):
            dest.write_bytes(b'video')
            return str(dest)

        for row in self.rows:
            row['start'] = 0
            row['end'] = 3
            row['duration'] = 3
        agent.STATE['files'][0]['audio'] = True
        settings = {'exportSrt':True,'srtFolder':str(captions),'coverEnabled':True}
        with patch.object(agent,'export_one',side_effect=render), \
             patch.object(agent.text_clean,'analyze_text',return_value=events), \
             patch.object(agent.text_clean,'voice_segments',return_value=[]):
            agent.export_episodes(self.rows,settings,self.output,lambda *args:None)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),['20.mp4','21.mp4'])
        self.assertEqual(sorted(p.name for p in captions.iterdir()),['20.srt','21.srt'])
        self.assertIn('00:00:01,800 --> 00:00:02,800',(captions/'20.srt').read_text())


if __name__ == '__main__':
    unittest.main()
