import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import agent


class PerformanceTests(unittest.TestCase):
    def test_parallel_analysis_keeps_source_order_and_episode_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            files=[]
            for n in (1,2,3):
                path=root/f'{n}.mp4'
                path.write_bytes(b'video')
                files.append({'path':str(path),'name':path.name,'duration':50,
                              'fps':25,'sourceNumber':n})
            old_cache,old_state=agent.CACHE,dict(agent.STATE)
            agent.CACHE=root/'cache'
            agent.CACHE.mkdir()
            agent.STATE.update(files=files,poster='',orderConfirmed=True)
            def finished_out_of_order(file):
                if file['sourceNumber']==1:time.sleep(.06)
                return [],0,50
            try:
                with patch.object(agent,'cached_analyse',side_effect=finished_out_of_order):
                    result=agent.plan({'startEpisode':20,'target':3,'minSeconds':45,
                                       'maxSeconds':60,'removeOld':False,
                                       'autoRemoveIntroCover':False},lambda *_:None)
                self.assertEqual([r['episode'] for r in result['plan']],[20,21,22])
                self.assertEqual([r['sourceNumber'] for r in result['plan']],[1,2,3])
            finally:
                agent.CACHE=old_cache
                agent.STATE.clear()
                agent.STATE.update(old_state)

    def test_analysis_cache_reuses_source_and_invalidates_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'1.mp4'
            source.write_bytes(b'original')
            file = {'path':str(source), 'duration':60, 'fps':25}
            old_cache = agent.CACHE
            agent.CACHE = root/'cache'
            try:
                with patch.object(agent,'analyse',return_value=([{'time':30,'score':3}],0,60)) as analyze:
                    self.assertEqual(agent.cached_analyse(file),agent.cached_analyse(file))
                    self.assertEqual(analyze.call_count,1)
                    source.write_bytes(b'modified source')
                    agent.cached_analyse(file)
                    self.assertEqual(analyze.call_count,2)
            finally:
                agent.CACHE = old_cache

    def test_poster_cache_reuses_result_and_invalidates_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, poster = root/'1.mp4',root/'poster.png'
            source.write_bytes(b'video')
            poster.write_bytes(b'first poster')
            file = {'path':str(source), 'duration':60, 'fps':25}
            old_cache = agent.CACHE
            agent.CACHE = root/'cache'
            try:
                with patch.object(agent,'detect_intro_cover_frames',return_value=3) as detect:
                    self.assertEqual(agent.cached_intro_cover_frames(file,str(poster)),3)
                    self.assertEqual(agent.cached_intro_cover_frames(file,str(poster)),3)
                    self.assertEqual(detect.call_count,1)
                    poster.write_bytes(b'changed poster')
                    agent.cached_intro_cover_frames(file,str(poster))
                    self.assertEqual(detect.call_count,2)
            finally:
                agent.CACHE = old_cache

    def test_encoder_modes_keep_spec_and_support_hardware_fallback(self):
        with patch.object(agent,'hardware_encoder_available',return_value=True):
            fast = agent.video_encoder_candidates({'encodingMode':'fast'})
            quality = agent.video_encoder_candidates({'encodingMode':'quality'})
        self.assertIn('h264_videotoolbox',fast[0])
        self.assertIn('libx264',fast[1])
        self.assertEqual(quality[0][quality[0].index('-preset')+1],'medium')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_cache,old_state = agent.CACHE,dict(agent.STATE)
            agent.CACHE=root/'cache'
            agent.CACHE.mkdir()
            agent.STATE.update(files=[{'path':str(root/'1.mp4'),'audio':False}],poster='')
            calls=[]
            def fake_run(command,timeout=None):
                calls.append(command)
                output=Path(command[-1])
                if 'h264_videotoolbox' in command:
                    output.write_bytes(b'partial')
                    raise RuntimeError('hardware encoder unavailable')
                self.assertFalse(output.exists())
                output.write_bytes(b'encoded')
            try:
                with patch.object(agent,'run',side_effect=fake_run),patch.object(
                        agent,'video_encoder_candidates',return_value=fast):
                    dest=root/'result.mp4'
                    agent.export_one({'episode':20,'source':0,'start':0,'end':1},
                                     {'encodingMode':'fast','coverEnabled':False,'tailEnabled':False},dest)
                    self.assertEqual(dest.read_bytes(),b'encoded')
                    self.assertEqual(len(calls),2)
                    self.assertIn('libx264',calls[1])
            finally:
                agent.CACHE=old_cache
                agent.STATE.clear()
                agent.STATE.update(old_state)

    def test_quality_mode_resumes_prior_video_and_srt_without_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'source.mp4'
            source.write_bytes(b'original')
            out,srt_dir=root/'video',root/'srt'
            out.mkdir();srt_dir.mkdir()
            row={'episode':20,'source':0,'start':0,'end':2,'duration':2}
            settings={'encodingMode':'quality','coverEnabled':False,'tailEnabled':False,
                      'exportSrt':True,'srtFolder':str(srt_dir)}
            legacy=dict(settings);legacy.pop('encodingMode')
            old_cache,old_state=agent.CACHE,dict(agent.STATE)
            agent.CACHE=root/'cache'
            agent.CACHE.mkdir()
            agent.STATE.update(files=[{'path':str(source),'audio':True}],poster='')
            try:
                sig=agent.render_signature(legacy,[row])
                video=out/'20.mp4';video.write_bytes(b'previous video')
                subtitle=srt_dir/'20.srt';subtitle.write_text('previous subtitle')
                agent.record_export_stamp(agent.export_stamp_path(out,20),video,sig)
                agent.record_export_stamp(agent.export_stamp_path(srt_dir,20),subtitle,sig)
                with patch.object(agent,'probe',return_value={'duration':3}),patch.object(
                        agent,'export_one',side_effect=AssertionError('should reuse video')),patch.object(
                        agent.text_clean,'analyze_text',side_effect=AssertionError('should reuse subtitle')):
                    result=agent.export_episodes([row],settings,out,lambda *_:None)
                self.assertEqual(result['files'],1)
                self.assertEqual(video.read_bytes(),b'previous video')
            finally:
                agent.CACHE=old_cache
                agent.STATE.clear()
                agent.STATE.update(old_state)


if __name__=='__main__':unittest.main()
