"""Local OCR, speech cross-check, and timed text removal for split episodes."""
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SAMPLE_INTERVAL = .5


def bounds(settings, kind):
    if kind == 'subtitle':
        top = float(settings.get('subtitleTopPct', 58)) / 100
        bottom = float(settings.get('subtitleBottomPct', 85)) / 100
    else:
        top = float(settings.get('nameTopPct', 15)) / 100
        bottom = float(settings.get('nameBottomPct', 58)) / 100
    if not 0 <= top < bottom <= 1:
        raise ValueError('文字识别区域的上下边界无效。')
    return top, bottom


def source_key(path, settings):
    stat = Path(path).stat()
    fields = [str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns,
              *bounds(settings, 'subtitle'), *bounds(settings, 'name'),
              bool(settings.get('removeNameBars', True)), SAMPLE_INTERVAL, 3]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def _tesseract_lines(frame, settings=None):
    """Development fallback; the Mac package supplies Apple's Vision OCR."""
    lines = []
    from PIL import Image
    image=Image.open(frame).convert('RGB')
    width,height=image.size
    for kind in ('subtitle','name'):
        if kind=='name' and not (settings or {}).get('removeNameBars',True):continue
        top,bottom=bounds(settings or {},kind)
        left=int(width*.03);upper=int(height*top);right=int(width*.97);lower=int(height*bottom)
        crop=image.crop((left,upper,right,lower))
        if kind=='subtitle':crop=crop.convert('L').point(lambda value:255 if value>205 else 0)
        with tempfile.NamedTemporaryFile(suffix='.png') as temp:
            crop.save(temp.name)
            result=subprocess.run(['tesseract',temp.name,'stdout','-l','eng','--psm',
                                   '6' if kind=='subtitle' else '11','tsv'],
                                  capture_output=True,text=True,check=True)
        grouped={}
        for line in result.stdout.splitlines()[1:]:
            cells=line.split('\t',11)
            if len(cells)<12 or cells[0]!='5' or not cells[11].strip():continue
            key=tuple(cells[1:5])
            grouped.setdefault(key,[]).append(cells)
        for words in grouped.values():
            x=min(int(w[6]) for w in words)+left
            y=min(int(w[7]) for w in words)+upper
            end_x=max(int(w[6])+int(w[8]) for w in words)+left
            end_y=max(int(w[7])+int(w[9]) for w in words)+upper
            conf=max(0,sum(float(w[10]) for w in words)/len(words)/100)
            lines.append(dict(text=' '.join(w[11] for w in words),confidence=conf,
                              x=x/width,y=y/height,w=(end_x-x)/width,h=(end_y-y)/height))
    return lines


def _normalize(text):
    return re.sub(r'[^\w]+', '', text.casefold())


def _merge_frames(frames, settings):
    active = {'subtitle': None, 'name': None}
    events = []
    for frame in frames:
        time = frame['index'] * SAMPLE_INTERVAL
        for kind in ('subtitle', 'name'):
            if kind == 'name' and not settings.get('removeNameBars', True):
                continue
            top, bottom = bounds(settings, kind)
            lines = [line for line in frame['lines']
                     if float(line.get('confidence', 0)) >= .46
                     and top <= float(line['y']) + float(line['h'])/2 <= bottom
                     and .009 <= float(line['h']) <= .07
                     and len(_normalize(line.get('text', ''))) >= 3]
            if not lines:
                continue
            lines.sort(key=lambda line: (line['y'], line['x']))
            value = '\n'.join(line['text'].strip() for line in lines)
            x = min(float(line['x']) for line in lines)
            y = min(float(line['y']) for line in lines)
            right = max(float(line['x']) + float(line['w']) for line in lines)
            lower = max(float(line['y']) + float(line['h']) for line in lines)
            previous = active[kind]
            similar = previous and time-previous['last'] <= .8 and abs(y-previous['y']) < .08 and \
                difflib.SequenceMatcher(None, _normalize(previous['text']), _normalize(value)).ratio() > .68
            if similar:
                previous['last'] = time
                previous['end'] = time + SAMPLE_INTERVAL
                previous['seen'] += 1
                previous['x'] = min(previous['x'], x)
                previous['y'] = min(previous['y'], y)
                previous['w'] = max(previous['right'], right) - previous['x']
                previous['h'] = max(previous['bottom'], lower) - previous['y']
                previous['right'] = max(previous['right'], right)
                previous['bottom'] = max(previous['bottom'], lower)
                if len(value) > len(previous['text']):
                    previous['text'] = value
            else:
                previous = dict(kind=kind, start=max(0, time-.15), end=time+SAMPLE_INTERVAL,
                                last=time, seen=1, text=value, x=x, y=y, right=right, bottom=lower,
                                w=right-x, h=lower-y)
                events.append(previous)
                active[kind] = previous
    return [{key: value for key, value in item.items() if key not in {'last', 'seen', 'right', 'bottom'}}
            for item in events if item['seen'] >= 2]


def analyze_text(path, settings, cache):
    cache = Path(cache) / 'text-analysis'
    cache.mkdir(parents=True, exist_ok=True)
    key = source_key(path, settings)
    saved = cache / f'{key}.json'
    if saved.is_file():
        return json.loads(saved.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='ocr-', dir=cache) as directory:
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(path),
                        '-an', '-vf', 'fps=2,scale=720:-2', '-q:v', '4',
                        str(Path(directory)/'%06d.jpg')], check=True, capture_output=True)
        frames = sorted(Path(directory).glob('*.jpg'))
        if not frames:
            raise RuntimeError('无法提取视频帧进行字幕识别。')
        if shutil.which('episode-ocr'):
            lang = 'zh-Hans' if settings.get('language') == 'zh' else 'en-US'
            result = subprocess.run(['episode-ocr', directory, lang],
                                    check=True, capture_output=True, text=True)
            recognized = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            if len(recognized) != len(frames):
                raise RuntimeError('文字识别结果与提取的画面数量不一致。')
        elif shutil.which('tesseract'):
            recognized = [dict(index=i, lines=_tesseract_lines(frame,settings))
                          for i, frame in enumerate(frames)]
        else:
            raise RuntimeError('应用缺少本机文字识别组件。')
        events = _merge_frames(recognized, settings)
    tmp = saved.with_suffix('.tmp')
    tmp.write_text(json.dumps(events, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, saved)
    return events


def voice_segments(path, cache, language='en'):
    if not shutil.which('whisper-cli'):
        raise RuntimeError('应用缺少本机语音识别组件。')
    model = Path(os.environ.get('EPISODE_WHISPER_MODEL', ''))
    if not model.is_file():
        raise RuntimeError('应用缺少离线语音模型，请重新安装。')
    language = language if language in {'en','zh'} else 'auto'
    stat = Path(path).stat()
    key = hashlib.sha256(json.dumps([str(Path(path).resolve()), stat.st_size,
                                     stat.st_mtime_ns, language, 'base-v1']).encode()).hexdigest()
    cache = Path(cache) / 'speech-analysis'
    cache.mkdir(parents=True, exist_ok=True)
    saved = cache / f'{key}.json'
    if saved.is_file():
        return json.loads(saved.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='speech-', dir=cache) as directory:
        wav = Path(directory) / 'audio.wav'
        out = Path(directory) / 'result'
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(path), '-vn',
                        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav)],
                       check=True, capture_output=True)
        subprocess.run(['whisper-cli', '-m', str(model), '-l', language,
                        '-f', str(wav), '-oj', '-of', str(out), '-np'],
                       check=True, capture_output=True)
        payload = json.loads(out.with_suffix('.json').read_text(encoding='utf-8'))
        segments = [dict(start=part['offsets']['from']/1000,
                         end=part['offsets']['to']/1000, text=part['text'].strip())
                    for part in payload.get('transcription', [])
                    if part.get('text', '').strip() and part.get('offsets')]
    tmp = saved.with_suffix('.tmp')
    tmp.write_text(json.dumps(segments, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, saved)
    return segments


def subtitle_cues(events, speech, row, cover_duration=0):
    start, end = float(row['start']), float(row['end'])
    ocr = [dict(begin=max(start, float(e['start'])), finish=min(end, float(e['end'])),
                text=e['text'], origin='画面字幕') for e in events
           if e['kind'] == 'subtitle' and e['end'] > start and e['start'] < end]
    cues = [cue for cue in ocr if cue['finish'] - cue['begin'] > .15]
    for segment in speech:
        a, b = max(start, segment['start']), min(end, segment['end'])
        if b-a < .3:
            continue
        # OCR wins whenever any substantial part of this speech overlaps a visible subtitle.
        if any(min(b, c['finish'])-max(a, c['begin']) > min(.5, (b-a)*.3)
               for c in cues):
            continue
        cues.append(dict(begin=a, finish=b, text=segment['text'], origin='语音补漏'))
    cues.sort(key=lambda cue: (cue['begin'], cue['finish']))
    return [dict(start=max(0, cue['begin']-start)+cover_duration,
                 end=min(end-start, cue['finish']-start)+cover_duration,
                 text=cue['text'], origin=cue['origin']) for cue in cues]


def format_srt(cues):
    def timestamp(time):
        millis = max(0, round(time*1000))
        hours, millis = divmod(millis, 3600000)
        minutes, millis = divmod(millis, 60000)
        seconds, millis = divmod(millis, 1000)
        return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}'
    return ''.join(f"{i}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n"
                   f"{c['text'].strip()}\n\n" for i, c in enumerate(cues, 1)
                   if c['end'] > c['start'])


def delogo_filters(events, row, settings):
    """Use detected text boxes only while the text is present; no full-frame blur."""
    start, end = float(row['start']), float(row['end'])
    filters = []
    for event in events:
        if event['end'] <= start or event['start'] >= end:
            continue
        pad_x = .012 if event['kind'] == 'subtitle' else .025
        pad_y = .008 if event['kind'] == 'subtitle' else .015
        x = max(0, int((event['x']-pad_x)*1080))
        y = max(0, int((event['y']-pad_y)*1920))
        right = min(1080, int((event['x']+event['w']+pad_x)*1080))
        bottom = min(1920, int((event['y']+event['h']+pad_y)*1920))
        if right-x < 8 or bottom-y < 8:
            continue
        a = max(0, event['start']-start-.3)
        b = min(end-start, event['end']-start+.3)
        filters.append(f"delogo=x={x}:y={y}:w={right-x}:h={bottom-y}:enable='between(t\\,{a:.3f}\\,{b:.3f})'")
    return filters
