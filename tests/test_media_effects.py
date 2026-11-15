import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from agent import agent


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MediaEffectsTests(unittest.TestCase):
    def ffmpeg(self, *args):
        subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', *map(str, args)],
                       check=True, capture_output=True, timeout=60)

    def test_single_cover_frame_is_detected_at_native_frame_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video, poster = root / 'intro.mp4', root / 'poster.png'
            Image.new('RGB', (180, 320), 'blue').save(poster)
            self.ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=180x320:r=30:d=0.0333333',
                        '-f', 'lavfi', '-i', 'color=c=red:s=180x320:r=30:d=1',
                        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
                        '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', video)
            info = agent.probe(video)
            self.assertAlmostEqual(info['fps'], 30, delta=.01)
            self.assertEqual(agent.detect_intro_cover_frames({'path':str(video), **info}, str(poster)), 1)

    def test_opaque_effect_preserves_source_frame_and_zoom_changes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, effect, output = root / 'source.mp4', root / 'effect.mp4', root / 'out.mp4'
            self.ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=180x320:r=25:d=1',
                        '-vf', 'drawbox=x=70:y=80:w=40:h=40:color=blue:t=fill',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', source)
            self.ffmpeg('-f', 'lavfi', '-i', 'color=c=black:s=180x320:r=25:d=1.6',
                        '-vf', 'drawbox=x=60:y=210:w=60:h=50:color=white:t=fill',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', effect)
            old_cache, old_state = agent.CACHE, dict(agent.STATE)
            try:
                agent.CACHE = root / 'cache'
                agent.CACHE.mkdir()
                agent.STATE.update(files=[{'path':str(source),'audio':False,'size':source.stat().st_size}],poster='')
                settings = {'coverEnabled':False,'tailEnabled':True,'tailDuration':1.6,
                            'tailStyle':'custom','tailVisualPath':str(effect),'tailBlendMode':'bright',
                            'tailBlendOpacity':40,'tailBlendCutoff':18,'tailBlendSoftness':12,
                            'zoomEnabled':True,'zoomStartPct':100,'zoomEndPct':140,
                            'preset':'ultrafast','crf':25}
                agent.export_one({'episode':20,'source':0,'start':0,'end':1},settings,output)
                meta=agent.probe(output)
                self.assertEqual((meta['width'],meta['height']), (1080,1920))
                self.assertTrue(meta['audio'])
                frames=[]
                for index,at in enumerate((1.16,2.32)):
                    frame=root/f'frame-{index}.png'
                    self.ffmpeg('-ss',at,'-i',output,'-frames:v','1',frame)
                    with Image.open(frame) as image:frames.append(image.convert('RGB'))
                outside=frames[-1].getpixel((100,100))
                inside=frames[-1].getpixel((500,1400))
                self.assertGreater(outside[0],170)
                self.assertLess(outside[1],70)  # black effect background did not cover the source
                self.assertGreater(inside[1],50)  # retained bright effect region
                difference=ImageStat.Stat(ImageChops.difference(frames[0],frames[1])).mean
                self.assertGreater(sum(difference),10)  # keyframe zoom changed the frozen frame
            finally:
                agent.CACHE=old_cache
                agent.STATE.clear()
                agent.STATE.update(old_state)


if __name__=='__main__':unittest.main()
