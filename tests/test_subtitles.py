import re
import shutil
import subprocess
import unittest

from agent import text_clean


class SubtitleTests(unittest.TestCase):
    def test_ocr_priority_speech_gap_and_split_timing(self):
        events = [
            {'kind':'subtitle','start':39.6,'end':41.2,'text':'Mom, what do we do?',
             'x':.15,'y':.70,'w':.70,'h':.03},
            {'kind':'name','start':40,'end':42,'text':'LUCIA',
             'x':.08,'y':.35,'w':.3,'h':.04},
            {'kind':'subtitle','start':43,'end':44.2,'text':'We need to leave.',
             'x':.17,'y':.70,'w':.66,'h':.03},
        ]
        speech = [
            {'start':39.7,'end':41.1,'text':'Mom what do we do'},
            {'start':42,'end':42.8,'text':'Now!'},
            {'start':43,'end':44.1,'text':'We need to leave'},
        ]
        row = {'start':40,'end':43.5}
        cues = text_clean.subtitle_cues(events,speech,row,.8)
        self.assertEqual([c['text'] for c in cues], ['Mom, what do we do?', 'Now!', 'We need to leave.'])
        self.assertEqual([c['origin'] for c in cues], ['画面字幕', '语音补漏', '画面字幕'])
        self.assertAlmostEqual(cues[0]['start'],.8)
        self.assertAlmostEqual(cues[-1]['end'],4.3)
        srt = text_clean.format_srt(cues)
        self.assertIn('00:00:00,800 --> 00:00:02,000',srt)
        self.assertIn('00:00:03,800 --> 00:00:04,300',srt)
        self.assertNotIn('LUCIA',srt)

    def test_removal_only_during_detected_text_and_inside_clip(self):
        events = [{'kind':'subtitle','start':10,'end':12,'x':.1,'y':.7,'w':.8,'h':.04}]
        filters = text_clean.delogo_filters(events,{'start':11,'end':14},{})
        self.assertEqual(len(filters),1)
        self.assertIn('between(t\\,0.000\\,1.300)',filters[0])
        self.assertIn('delogo=x=',filters[0])
        self.assertEqual(text_clean.delogo_filters(events,{'start':20,'end':22},{}),[])

    def test_delogo_never_touches_frame_edges(self):
        # OCR may report a word beyond the edge or a name bar reaching the corner.
        events = [
            {'kind':'subtitle','start':0,'end':1,'x':-.02,'y':.3,'w':.3,'h':.04},
            {'kind':'name','start':0,'end':1,'x':.95,'y':.95,'w':.2,'h':.1},
            {'kind':'subtitle','start':0,'end':1,'x':1.15,'y':.5,'w':.2,'h':.02},
        ]
        filters = text_clean.delogo_filters(events,{'start':0,'end':1},{})
        self.assertEqual(len(filters),2)
        for item in filters:
            x,y,w,h = map(int,re.search(r'delogo=x=(\d+):y=(\d+):w=(\d+):h=(\d+)',item).groups())
            self.assertGreater(x,0)
            self.assertGreater(y,0)
            self.assertLess(x+w,1080)
            self.assertLess(y+h,1920)
        if shutil.which('ffmpeg'):
            result = subprocess.run([
                'ffmpeg','-hide_banner','-v','error','-f','lavfi','-i',
                'color=s=1080x1920:r=1:d=1','-vf',','.join(filters),
                '-frames:v','1','-f','null','-'
            ],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_merges_matching_frames_but_excludes_name_bar_from_srt(self):
        line = lambda text,y: {'text':text,'confidence':.94,'x':.12,'y':y,'w':.72,'h':.03}
        frames = [{'index':i,'lines':[line('That was my brother',.70),line('LUCIA',.32)]}
                  for i in range(3)]
        events = text_clean._merge_frames(frames,{})
        self.assertEqual(len(events),2)
        cues = text_clean.subtitle_cues(events,[],{'start':0,'end':3})
        self.assertEqual(len(cues),1)
        self.assertEqual(cues[0]['text'],'That was my brother')


if __name__ == '__main__':unittest.main()

