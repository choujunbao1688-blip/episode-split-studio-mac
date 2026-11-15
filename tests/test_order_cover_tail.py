import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops
from agent import agent


class OrderCoverTailTests(unittest.TestCase):
    def test_numeric_source_order_and_story_cuts_do_not_force_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('成片_10.mp4', '20261112_2.mp4', '1.mp4'):
                (root / name).touch()
            original_cache = agent.CACHE
            original_state = dict(agent.STATE)
            try:
                agent.CACHE = root / 'cache'
                with patch.object(agent, 'probe', return_value={'duration': 70, 'width': 1080, 'height': 1920, 'fps':25, 'audio': True}):
                    files = agent.scan(root, '')['files']
                self.assertEqual([f['name'] for f in files], ['1.mp4', '20261112_2.mp4', '成片_10.mp4'])
                self.assertEqual([f['sourceNumber'] for f in files], [1, 2, 10])
                self.assertTrue(agent.STATE['orderConfirmed'])
                # A gap is flagged, but does not prevent use of the imported order.
                self.assertNotEqual(files[2]['sourceNumber'], files[1]['sourceNumber'] + 1)
                with patch.object(agent, 'analyse', return_value=([], 0, 70)):
                    result = agent.plan({'startEpisode': 20, 'minSeconds': 45, 'maxSeconds': 60, 'target': 90}, lambda *_: None)
                self.assertEqual([(r['episode'], r['file'], r['segment']) for r in result['plan']],
                                 [(20, '1.mp4', 1), (21, '20261112_2.mp4', 1), (22, '成片_10.mp4', 1)])
                strong = [{'time': 56, 'score': 3.1, 'reason': '剧情切换'}]
                self.assertEqual([c['time'] for c in agent.story_cuts(strong, 0, 140, 45, 60)], [56])
            finally:
                agent.CACHE = original_cache
                agent.STATE.clear()
                agent.STATE.update(original_state)

    def test_restores_previous_state_in_numeric_order(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('10.mp4', '2.mp4', '1.mp4'):
                (root / name).touch()
            original_cache = agent.CACHE
            original_state = dict(agent.STATE)
            try:
                agent.CACHE = root / 'cache'
                agent.CACHE.mkdir()
                state = {'files': [{'path': str(root / name), 'name': name, 'duration': 60}
                                   for name in ('1.mp4', '10.mp4', '2.mp4')],
                         'plan': [{'episode': 20, 'file': '10.mp4'}], 'orderConfirmed': True}
                (agent.CACHE / 'state.json').write_text(json.dumps(state))
                agent.restore()
                self.assertEqual([f['name'] for f in agent.STATE['files']], ['1.mp4', '2.mp4', '10.mp4'])
                self.assertEqual(agent.STATE['plan'], [])
                self.assertTrue(agent.STATE['orderConfirmed'])
            finally:
                agent.CACHE = original_cache
                agent.STATE.clear()
                agent.STATE.update(original_state)

    def test_cover_uses_percent_coordinates_in_rendered_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            poster, output = root / 'poster.png', root / 'cover.png'
            Image.new('RGB', (1080, 1920), '#69503e').save(poster)
            original_poster = agent.STATE.get('poster')
            try:
                agent.STATE['poster'] = str(poster)
                agent.cover(output, 20, {'coverEnabled': True, 'numberFormat': 'EP{n}',
                                         'fontSize': 100, 'positionX': 49, 'positionY': 58})
                with Image.open(poster) as base, Image.open(output) as rendered:
                    area = (280, 950, 780, 1300)
                    self.assertIsNotNone(ImageChops.difference(base.crop(area), rendered.crop(area)).getbbox())
            finally:
                agent.STATE['poster'] = original_poster

    def test_custom_visual_accepts_opaque_video_layer_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transparent, opaque = root / 'overlay.png', root / 'scene.png'
            Image.new('RGBA', (9, 16), (255, 220, 160, 80)).save(transparent)
            Image.new('RGB', (9, 16), (255, 220, 160)).save(opaque)
            self.assertEqual(agent.checked_effect_visual(str(transparent)), str(transparent.resolve()))
            self.assertTrue(agent.visual_has_alpha(str(transparent)))
            self.assertEqual(agent.checked_effect_visual(str(opaque)), str(opaque.resolve()))
            self.assertFalse(agent.visual_has_alpha(str(opaque)))


if __name__ == '__main__':
    unittest.main()
