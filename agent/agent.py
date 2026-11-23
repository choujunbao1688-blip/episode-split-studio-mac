#!/usr/bin/env python3
"""Episode Split Studio local processor. Python 3.10+, FFmpeg, Pillow."""
import argparse, array, base64, hashlib, hmac, json, math, os, re, secrets, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageStat, ImageOps
try:
 from . import text_clean
except ImportError:
 import text_clean

VIDEO_EXT={'.mp4','.mov','.mkv','.m4v','.avi'}
STATE={'files':[], 'plan':[], 'settings':{}, 'job':None, 'samples':{}, 'sampleCovers':{}, 'tailPreviews':{}, 'warnings':[], 'orderConfirmed':False}
PLAN_FIELDS=('target','startEpisode','minSeconds','maxSeconds','language','removeOld','trimIntroSeconds','trimIntroFrames','autoRemoveIntroCover')
LOCK=threading.RLock()
TOKEN=secrets.token_urlsafe(18)
HOME=Path.home()
CACHE=HOME/'.episode-split-studio'
CACHE.mkdir(exist_ok=True)
FONT_DIR=Path(__file__).parent/'fonts'
FONT_CHOICES={
 'Libre Baskerville':[FONT_DIR/'DejaVuSerif-BoldItalic.ttf','/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf'],
 'Tinos':[FONT_DIR/'DejaVuSerif-Bold.ttf','/System/Library/Fonts/Supplemental/Times New Roman.ttf'],
 'STIX Two Text':[FONT_DIR/'DejaVuSerif-Bold.ttf','/System/Library/Fonts/Supplemental/Georgia Bold.ttf']}

def run(cmd, timeout=None):
 p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
 if p.returncode: raise RuntimeError((p.stderr or p.stdout)[-1400:])
 return p.stdout

def probe(path):
 d=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=120))
 v=next((x for x in d['streams'] if x['codec_type']=='video'),None)
 if not v: raise ValueError('没有视频流')
 dur=float(d['format'].get('duration') or v.get('duration') or 0)
 if dur<=0: raise ValueError('无法读取时长')
 try:fps=float(Fraction(v.get('avg_frame_rate') or v.get('r_frame_rate') or '25'))
 except (ValueError,ZeroDivisionError):fps=25
 return {'duration':round(dur,3),'width':v.get('width'), 'height':v.get('height'), 'fps':fps or 25,'audio':any(x['codec_type']=='audio' for x in d['streams'])}

def sortkey(p):
 number=source_number(p.name)
 parts=tuple(int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)',p.stem))
 return (0,number,parts,p.name.lower()) if number is not None else (1,0,parts,p.name.lower())

def source_number(name):
 stem=Path(name).stem
 m=re.search(r'(?i)(?:EP|第)\s*0*(\d+)',stem)
 if m:return int(m.group(1))
 # Common exports include a title or date before the episode number.
 m=re.search(r'(?:^|\D)0*(\d+)\s*(?:\(\d+\))?$',stem)
 if m:return int(m.group(1))
 m=re.match(r'^\s*0*(\d+)(?:\D|$)',stem)
 return int(m.group(1)) if m else None

def checked_poster(poster):
 if not poster:return ''
 q=Path(poster).expanduser().resolve()
 if not q.is_file() or q.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:raise ValueError('海报须为本机可读取的 PNG、JPG 或 WebP。')
 Image.open(q).verify()
 return str(q)

def checked_effect_audio(value):
 if not value:return ''
 path=Path(value).expanduser().resolve()
 if not path.is_file() or path.suffix.lower() not in {'.mp3','.wav','.m4a','.aac','.flac'}:raise ValueError('音效须为本机可读取的 MP3、WAV、M4A、AAC 或 FLAC。')
 streams=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=30))
 if not any(x.get('codec_type')=='audio' for x in streams.get('streams',[])):raise ValueError('音效文件中没有音频流。')
 return str(path)

def checked_effect_visual(value):
 if not value:return ''
 path=Path(value).expanduser().resolve()
 if not path.is_file() or path.suffix.lower() not in {'.png','.webp','.mp4','.m4v','.mov','.webm'}:
  raise ValueError('画面素材须为本机可读取的 PNG、WebP、MP4、MOV 或 WebM。')
 if path.suffix.lower() in {'.png','.webp'}:
  with Image.open(path) as im:im.verify()
 else:
  info=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=pix_fmt','-of','json',str(path)],timeout=30))
  if not info.get('streams'):raise ValueError('画面素材没有视频流。')
 return str(path)

def visual_has_alpha(path):
 if Path(path).suffix.lower() in {'.png','.webp'}:
  with Image.open(path) as im:return 'A' in im.getbands() or 'transparency' in im.info
 info=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=pix_fmt','-of','json',str(path)],timeout=30))
 pix=next((x.get('pix_fmt','') for x in info.get('streams',[])),'')
 return any(x in pix for x in ('yuva','rgba','bgra','argb','abgr','gbrap'))

def scan(folder,poster):
 root=Path(folder).expanduser().resolve()
 if not root.is_dir(): raise ValueError('剧集目录不存在。NAS 请先在电脑上挂载，再输入挂载目录。')
 paths=sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT),key=sortkey)
 if not paths: raise ValueError('该目录中没有可读取的视频。')
 files=[]; warnings=[]
 for i,p in enumerate(paths):
  try: meta=probe(p); files.append({'path':str(p),'name':p.name,'size':p.stat().st_size,'sourceNumber':source_number(p.name),'tailTrim':0,'headTrimFrames':0,**meta})
  except Exception as e: warnings.append(f'{p.name}: {e}')
 if not files: raise ValueError('没有成功读取的视频。')
 numbers=[f['sourceNumber'] for f in files if f['sourceNumber'] is not None]
 if len(numbers)!=len(set(numbers)): warnings.append('源文件存在相同数字编号；请核对并排除重复文件。')
 if numbers and len(numbers)==len(files) and max(numbers)-min(numbers)+1!=len(set(numbers)): warnings.append('源文件编号存在缺集或重复。')
 for i,f in enumerate(files):f['sourceOrdinal']=i+1
 poster_path=checked_poster(poster)
 with LOCK: STATE.update(files=files,poster=poster_path,plan=[],planParams=None,warnings=warnings,orderConfirmed=True,sourceOrderMode='auto',samples={},sampleCovers={},sampleSignature=None)
 persist()
 return {'files':files,'warnings':warnings,'duration':sum(f['duration'] for f in files),'size':sum(f['size'] for f in files)}

def persist():
 with LOCK:
  data={k:STATE.get(k) for k in ('files','poster','plan','planParams','settings','warnings','orderConfirmed','sourceOrderMode')}
  CACHE.mkdir(parents=True,exist_ok=True)
  fd,name=tempfile.mkstemp(prefix='state-',suffix='.tmp',dir=CACHE)
  try:
   with os.fdopen(fd,'w',encoding='utf-8') as output:
    json.dump(data,output,ensure_ascii=False)
    output.flush();os.fsync(output.fileno())
   os.replace(name,CACHE/'state.json')
  finally:
   Path(name).unlink(missing_ok=True)

def restore():
 try:
  d=json.loads((CACHE/'state.json').read_text())
  if all(Path(f['path']).exists() for f in d.get('files',[])):
   if d.get('plan') and any(k not in (d.get('planParams') or {}) for k in PLAN_FIELDS):
    d.update(plan=[],planParams=None)
   legacy='sourceOrderMode' not in d
   if d.get('sourceOrderMode')!='manual':
    old=[f['path'] for f in d.get('files',[])]
    d['files']=sorted(d.get('files',[]),key=lambda f:sortkey(Path(f['name'])))
    for i,f in enumerate(d['files']):f.update(sourceNumber=source_number(f['name']),sourceOrdinal=i+1)
    d['sourceOrderMode']='auto';d['orderConfirmed']=bool(d['files'])
    if old!=[f['path'] for f in d['files']] or legacy:
     d.update(plan=[],planParams=None)
   STATE.update(d)
   persist()
 except Exception:pass

def analyse(f):
 path=f['path']; duration=f['duration']; step=.25; energies=[]
 # A low-rate mono stream keeps memory proportional to the episode duration, not file size.
 if f['audio']:
  p=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-i',path,'-vn','-ac','1','-ar','4000','-f','s16le','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  n=1000
  while True:
   b=p.stdout.read(n*2)
   if not b: break
   a=array.array('h'); a.frombytes(b[:len(b)//2*2])
   energies.append(math.sqrt(sum(x*x for x in a)/max(1,len(a)))/32768)
  p.wait()
 # Scene cut timestamps via FFmpeg metadata. Sparse decoding for speed.
 scenes=[]
 try:
  p=subprocess.run(['ffmpeg','-nostdin','-v','info','-i',path,'-vf',"fps=2,scale=320:-1,select='gt(scene,0.28)',showinfo",'-an','-f','null','-'],capture_output=True,text=True)
  scenes=[float(x) for x in re.findall(r'pts_time:([\d.]+)',p.stderr)]
 except Exception: pass
 freezes=[]
 try:
  p=subprocess.run(['ffmpeg','-nostdin','-v','info','-sseof',str(-min(duration,9)),'-i',path,'-vf','freezedetect=n=-45dB:d=0.7','-an','-f','null','-'],capture_output=True,text=True)
  offset=max(0,duration-9)
  freezes=[offset+float(x) for x in re.findall(r'freeze_start: ([\d.]+)',p.stderr)]
 except Exception: pass
 tail=min((t for t in freezes if t>duration-7),default=duration)
 intro=0
 try:
  p=subprocess.run(['ffmpeg','-nostdin','-v','info','-t','5','-i',path,'-vf','freezedetect=n=-45dB:d=0.55','-an','-f','null','-'],capture_output=True,text=True)
  match=re.search(r'freeze_start: ([\d.]+).*?freeze_end: ([\d.]+)',p.stderr,re.S)
  if match and float(match.group(1))<.3 and .55<float(match.group(2))<4:intro=float(match.group(2))
 except Exception:pass
 candidates=[]
 for j in range(2,max(2,len(energies)-2)):
  t=j*step
  if t<intro+7 or t>tail-7: continue
  prev=max(energies[j-2:j+1]); after=max(energies[j+1:j+4],default=0)
  quiet=1-min(1,energies[j]*15)
  change=min(1,abs(after-prev)*9)
  peak=min(1,energies[j]*8) if energies[j]>=max(energies[j-2:j+3]) else 0
  scene=1 if any(abs(t-s)<.4 for s in scenes) else 0
  score=1.5*quiet+1.25*change+.65*peak+.8*scene
  if score>=.8: candidates.append({'time':round(t,2),'score':round(score,2),'reason':'镜头切换' if scene else ('语气变化' if change>.5 else '语音停顿')})
 candidates.sort(key=lambda c:c['score'],reverse=True)
 kept=[]
 for c in candidates:
  if all(abs(c['time']-x['time'])>2 for x in kept): kept.append(c)
 return sorted(kept,key=lambda x:x['time']),round(intro,2),round(tail,2)

def analysis_cache_key(f, poster=''):
 parts=[]
 for path in (f['path'],poster):
  if path:
   q=Path(path);stat=q.stat()
   parts.append((str(q.resolve()),stat.st_size,stat.st_mtime_ns))
 return hashlib.sha256(json.dumps([4,parts,f.get('duration'),f.get('fps')]).encode()).hexdigest()

def cached_analyse(f):
 folder=CACHE/'analysis';folder.mkdir(parents=True,exist_ok=True)
 path=folder/f'{analysis_cache_key(f)}.json'
 if path.is_file():
  try:
   data=json.loads(path.read_text(encoding='utf-8'))
   if len(data)==3:return data[0],data[1],data[2]
  except (OSError,ValueError,TypeError):pass
 result=analyse(f)
 temp=folder/f'.{secrets.token_hex(8)}.tmp'
 try:
  temp.write_text(json.dumps(result),encoding='utf-8')
  os.replace(temp,path)
 finally:temp.unlink(missing_ok=True)
 return result

def detect_intro_cover_frames(f,poster,max_seconds=4):
 """Compare native source frames to the selected clean poster, including one-frame covers."""
 if not poster:return 0
 fps=float(f.get('fps') or probe(f['path'])['fps'])
 count=min(240,max(1,round(fps*max_seconds)));w,h=160,284
 with Image.open(poster) as image:
  reference=ImageOps.fit(image.convert('L'),(w,h),method=Image.Resampling.LANCZOS).tobytes()
 command=['ffmpeg','-nostdin','-v','error','-i',f['path'],'-an','-vf',f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},format=gray','-frames:v',str(count),'-f','rawvideo','-']
 try:
  p=subprocess.run(command,capture_output=True,timeout=60)
  if p.returncode:return 0
 except (subprocess.SubprocessError,OSError):return 0
 frame_size=w*h;matches=0
 for offset in range(0,len(p.stdout)-frame_size+1,frame_size):
  frame=memoryview(p.stdout)[offset:offset+frame_size]
  error=sum(abs(frame[i]-reference[i]) for i in range(0,frame_size,16))/(frame_size/16)
  if error>26:break
  matches+=1
 return matches

def cached_intro_cover_frames(f,poster):
 if not poster:return 0
 folder=CACHE/'poster-analysis';folder.mkdir(parents=True,exist_ok=True)
 path=folder/f'{analysis_cache_key(f,poster)}.json'
 if path.is_file():
  try:return int(json.loads(path.read_text(encoding='utf-8')))
  except (OSError,ValueError,TypeError):pass
 count=detect_intro_cover_frames(f,poster)
 temp=folder/f'.{secrets.token_hex(8)}.tmp'
 try:
  temp.write_text(json.dumps(count),encoding='utf-8')
  os.replace(temp,path)
 finally:temp.unlink(missing_ok=True)
 return count

def story_cuts(cands,start,end,minlen,maxlen,threshold=2.45):
 """Keep source boundaries and split only at a strong eligible content cue."""
 out=[];left=start
 while end-left>maxlen*1.35:
  eligible=[c for c in cands if left+minlen<=c['time']<=min(left+maxlen*1.2,end-minlen)
            and c['score']>=threshold]
  if not eligible:break
  c=max(eligible,key=lambda x:x['score']-abs(x['time']-left-maxlen)/maxlen*.65)
  out.append({'time':c['time'],'reason':c['reason'],'confidence':min(99,round(42+c['score']*13)),'locked':False})
  left=c['time']
 return out

def plan_key(settings):
 return {name:settings.get(name) for name in PLAN_FIELDS}

def plan(settings, progress):
 with LOCK:
  files=list(STATE['files']);confirmed=STATE.get('orderConfirmed',False);poster=STATE.get('poster')
 if not files: raise ValueError('请先导入剧集。')
 if not confirmed:raise ValueError('请先在导入页确认原集顺序。')
 minlen=max(10,float(settings.get('minSeconds',45)));maxlen=max(minlen,float(settings.get('maxSeconds',60)))
 target=max(1,int(settings.get('target',len(files))))
 def analyze_source(f):
  candidates,intro,end=cached_analyse(f)
  if not settings.get('removeOld',True):intro=0;end=f['duration']
  fps=float(f.get('fps') or probe(f['path'])['fps'])
  cover_frames=cached_intro_cover_frames(f,poster) if settings.get('autoRemoveIntroCover',True) else 0
  frame_skip=max(0,int(settings.get('trimIntroFrames',0)),int(f.get('headTrimFrames',0)))
  intro=max(intro,cover_frames/fps,frame_skip/fps,float(settings.get('trimIntroSeconds',0)))
  if float(f.get('tailTrim',0))>0:end=min(end,f['duration']-float(f['tailTrim']))
  if end-intro<4:end=f['duration'];intro=0
  return candidates,intro,end
 analyses=[None]*len(files)
 with ThreadPoolExecutor(max_workers=min(2,len(files))) as pool:
  futures={pool.submit(analyze_source,f):i for i,f in enumerate(files)}
  for completed,future in enumerate(as_completed(futures),1):
   i=futures[future];analyses[i]=future.result()
   progress(f'已分析 {completed}/{len(files)} · {files[i]["name"]}',completed/len(files)*75)
 rows=[]; startnum=max(1,int(settings.get('startEpisode',1)))
 threshold=2.45 if target>len(files) else 2.7
 for i,(f,(cands,intro,end)) in enumerate(zip(files,analyses)):
  cuts=story_cuts(cands,intro,end,minlen,maxlen,threshold)
  points=[intro]+[x['time'] for x in cuts]+[end]
  for j in range(len(points)-1):
   duration=points[j+1]-points[j]
   rows.append({'id':len(rows)+1,'episode':startnum+len(rows),'source':i,'sourceOrdinal':i+1,'sourceNumber':f.get('sourceNumber'),'segment':j+1,'segmentsInSource':len(points)-1,'file':f['name'],'start':round(points[j],2),'end':round(points[j+1],2),'duration':round(duration,2),'cutReason':cuts[j]['reason'] if j<len(cuts) else '原集结尾','confidence':cuts[j]['confidence'] if j<len(cuts) else 90,'review':duration<minlen or duration>maxlen or (j<len(cuts) and cuts[j]['confidence']<40)})
 with LOCK: STATE.update(plan=rows,planParams=plan_key(settings),settings=settings,analyses=[{'candidates':c,'cleanStart':a,'cleanEnd':e} for c,a,e in analyses],samples={},sampleCovers={},sampleSignature=None)
 persist()
 progress('分析完成',100)
 return {'plan':rows,'count':len(rows),'target':target,'warnings':STATE['warnings']}

def font_for(name):
 for p in FONT_CHOICES.get(name,[])+FONT_CHOICES['Tinos']:
  if Path(p).exists(): return p
 raise RuntimeError('找不到字体文件，请安装 Libre Baskerville、Tinos 或 DejaVu Serif。')

def cover(path,episode,s):
 poster=STATE.get('poster')
 if not poster: return None
 im=Image.open(poster).convert('RGB'); w,h=1080,1920
 ratio=max(w/im.width,h/im.height); im=im.resize((round(im.width*ratio),round(im.height*ratio)),Image.Resampling.LANCZOS)
 x=(im.width-w)//2;y=(im.height-h)//2;im=im.crop((x,y,x+w,y+h)).convert('RGBA')
 if s.get('coverEnabled',True):
  label=str(s.get('numberFormat','EP{n}')).replace('{n}',str(episode))
  size=max(25,min(240,int(s.get('fontSize',100))))
  font=ImageFont.truetype(font_for(s.get('font','Libre Baskerville')),size)
  overlay=Image.new('RGBA',(w,h)); d=ImageDraw.Draw(overlay)
  spacing=max(-10,min(30,int(s.get('spacing',0))))
  box=d.textbbox((0,0),label,font=font,stroke_width=0);tw=box[2]-box[0]+spacing*max(0,len(label)-1);th=box[3]-box[1]
  def draw_spaced(draw,xy,fill,stroke_width=0):
   xx,yy=xy
   for char in label:
    draw.text((xx,yy),char,font=font,fill=fill,stroke_width=stroke_width,stroke_fill=fill)
    xx+=draw.textlength(char,font=font)+spacing
  px=int(w*max(0,min(100,float(s.get('positionX',50))))/100)
  py=int(h*max(0,min(100,float(s.get('positionY',26))))/100)
  pos=(int(px-tw/2),int(py-th/2-box[1]))
  shadow=int(s.get('shadowBlur',12)); dx=int(s.get('shadowX',14));dy=int(s.get('shadowY',4))
  if s.get('shadowEnabled',True):
   sh=Image.new('RGBA',(w,h));draw_spaced(ImageDraw.Draw(sh),(pos[0]+dx,pos[1]+dy),(0,0,0,255))
   if shadow: sh=sh.filter(ImageFilter.GaussianBlur(min(40,shadow)))
   overlay=Image.alpha_composite(overlay,sh)
  c1=s.get('colorStart','#fff4df');c2=s.get('colorEnd','#d49b43')
  def rgb(c):
   try:return tuple(bytes.fromhex(c.lstrip('#'))[:3])
   except:return (255,255,255)
  a,b=rgb(c1),rgb(c2)
  mask=Image.new('L',(w,h));draw_spaced(ImageDraw.Draw(mask),pos,255,stroke_width=max(0,int(s.get('stroke',0))))
  grad=Image.new('RGBA',(w,1))
  for xx in range(w):
   z=xx/w if s.get('gradient',True) else 0
   grad.putpixel((xx,0),tuple(round(a[k]*(1-z)+b[k]*z) for k in range(3))+(255,))
  grad=grad.resize((w,h));overlay.paste(grad,(0,0),mask)
  im=Image.alpha_composite(im,overlay)
 im.convert('RGB').save(path)
 return path

def make_rise(path,duration=2.2):
 sr=44100;n=int(sr*duration)
 with wave.open(str(path),'wb') as w:
  w.setnchannels(2);w.setsampwidth(2);w.setframerate(sr)
  buf=bytearray();phase=0
  for i in range(n):
   u=i/n;phase+=2*math.pi*(180+1400*u*u)/sr
   v=int(8500*(u**1.3)*min(1,(1-u)*15)*math.sin(phase))
   buf+=v.to_bytes(2,'little',signed=True)*2
  w.writeframes(buf)

def make_glow(path):
 if path.exists():return path
 glow=Image.new('RGBA',(1080,1920),(0,0,0,0))
 a=Image.new('RGBA',glow.size);d=ImageDraw.Draw(a)
 d.ellipse((50,270,780,1530),fill=(255,222,173,200))
 d.ellipse((380,570,1050,1510),fill=(190,164,255,105))
 a=a.filter(ImageFilter.GaussianBlur(125))
 glow=Image.alpha_composite(glow,a);glow.save(path)
 return path

def bounded(value,default,lo,hi):
 try:return max(lo,min(hi,float(value)))
 except (ValueError,TypeError):return default

def zoom_filter(settings,duration,tail):
 """Two or three scale keyframes, measured from the beginning of the freeze."""
 start=bounded(settings.get('zoomStartPct',100),100,100,200)/100
 middle=bounded(settings.get('zoomMidPct',112),112,100,200)/100
 end=bounded(settings.get('zoomEndPct',120),120,100,200)/100
 initial=round(duration*25);total=max(1,round(tail*25))
 def progress(offset,length):
  return f'min(max((on-{offset})/{max(1,length)}\\,0)\\,1)'
 if settings.get('zoomMidEnabled',False):
  mid=max(1,min(total-1,round(total*bounded(settings.get('zoomMidAt',50),50,5,95)/100)))
  expression=f'{start}+({middle-start})*{progress(initial,mid)}+({end-middle})*{progress(initial+mid,total-mid)}'
 else:expression=f'{start}+({end-start})*{progress(initial,total)}'
 return f"zoompan=z='{expression}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=25"

def render_assets(settings):
 assets=[]
 for p in [STATE.get('poster') if settings.get('coverEnabled',True) else None,
           settings.get('tailAudioPath') if settings.get('tailEnabled',True) else None,
           settings.get('tailVisualPath') if settings.get('tailEnabled',True) and settings.get('tailStyle')=='custom' else None]:
  if p:
   q=Path(p);assets.append((str(q),q.stat().st_size,q.stat().st_mtime_ns))
 for f in STATE['files']:
  q=Path(f['path']);assets.append((str(q),q.stat().st_size,q.stat().st_mtime_ns))
 return assets

def render_signature(settings,rows=None,assets=None):
 rows=rows if rows is not None else STATE['plan']
 if assets is None:assets=render_assets(settings)
 return hashlib.sha256(json.dumps([rows,settings,assets],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

@lru_cache(maxsize=1)
def hardware_encoder_available():
 try:
  encoders=run(['ffmpeg','-hide_banner','-encoders'],timeout=15)
  return bool(re.search(r'^\s*V\S*\s+h264_videotoolbox\b',encoders,re.M))
 except (OSError,RuntimeError,subprocess.TimeoutExpired):return False

def video_encoder_candidates(settings):
 if settings.get('encodingMode','fast')=='quality':
  return [['-c:v','libx264','-preset',settings.get('preset','medium'),
           '-crf',str(settings.get('crf',19))]]
 software=['-c:v','libx264','-preset',settings.get('preset','veryfast'),
           '-crf',str(settings.get('crf',18))]
 if hardware_encoder_available():
  return [['-c:v','h264_videotoolbox','-b:v','12M',
           '-maxrate','16M','-bufsize','24M'],software]
 return [software]

def export_one(row,s,out,job=None):
 f=STATE['files'][row['source']];start=float(row['start']);dur=float(row['end'])-start
 if dur<=0: raise ValueError('切点顺序错误。')
 work=CACHE/'render';work.mkdir(exist_ok=True)
 coverpath=work/f'cover-{row["episode"]}.png';poster=cover(coverpath,row['episode'],s)
 if s.get('coverEnabled',True) and not poster:raise ValueError('请先选择纯净海报，或关闭分集封面。')
 include_cover=bool(poster and s.get('coverEnabled',True));tail=bounded(s.get('tailDuration',2.8),2.8,.6,6) if s.get('tailEnabled',True) else 0
 style=s.get('tailStyle','warmflare') if s.get('tailStyle') in {'warmflare','flash','freeze','custom'} else 'warmflare'
 clip='setpts=PTS-STARTPTS,fps=25,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p'
 if s.get('removeText',False):
  text_events=text_clean.analyze_text(f['path'],s,CACHE)
  filters=text_clean.delogo_filters(text_events,row,s)
  if filters:clip+=','+','.join(filters)
 if tail:clip+=f',tpad=stop_mode=clone:stop_duration={tail}'
 if tail and s.get('zoomEnabled',False):clip+=','+zoom_filter(s,dur,tail)
 inputs=['-ss',str(start),'-t',str(dur),'-i',f['path']]
 if include_cover:inputs+=['-loop','1','-t','0.8','-i',str(coverpath)]
 sound_index=None
 if tail:
  sound=checked_effect_audio(s.get('tailAudioPath',''))
  if not sound:
   sound=work/'rise.wav'
   if not sound.exists():make_rise(sound)
  sound_index=2 if include_cover else 1
  inputs+=['-i',str(sound)]
 glow_index=None
 if tail and style=='warmflare':
  glow_index=len([f['path'],*([str(coverpath)] if include_cover else []),str(sound)])
  inputs+=['-loop','1','-t',str(tail),'-i',str(make_glow(work/'warm-glow.png'))]
 visual_index=None
 visual_alpha=False
 if tail and style=='custom':
  visual=checked_effect_visual(s.get('tailVisualPath',''))
  if not visual:raise ValueError('自定义画面效果需选择视频或图片素材。')
  visual_alpha=visual_has_alpha(visual)
  visual_index=len([f['path'],*([str(coverpath)] if include_cover else []),str(sound)])
  if Path(visual).suffix.lower() in {'.png','.webp'}:inputs+=['-loop','1','-t',str(tail),'-i',visual]
  else:inputs+=['-stream_loop','-1','-t',str(tail),'-i',visual]
 filters=[f'[0:v]{clip}[vclip]']
 fade=bounded(s.get('tailOriginalFade',.25),.25,0,2)
 if f['audio']:
  original=f'[0:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={dur},asetpts=PTS-STARTPTS'
  if tail and fade:original+=f',afade=t=out:st={max(0,dur-fade)}:d={min(fade,dur)}'
  filters.append(original+f',apad,atrim=duration={dur+tail}[abase]')
 else:filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={dur+tail}[abase]')
 if tail:
  offset=bounded(s.get('tailAudioOffset',.1),.1,0,5)
  gain=bounded(s.get('tailAudioVolume',80),80,0,150)/100
  delay=max(0,int((dur+min(offset,tail))*1000))
  filters.append(f'[{sound_index}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain},adelay={delay}|{delay},atrim=duration={dur+tail}[rise]')
  filters.append('[abase][rise]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.96[aclip]')
 else: filters.append('[abase]anull[aclip]')
 video='[vclip]'
 if glow_index is not None:
  strength=bounded(s.get('tailLightStrength',55),55,0,100)/100
  filters.append(f'[{glow_index}:v]format=rgba,fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st={max(.25,tail-.45)}:d={min(.45,tail-.25)}:alpha=1,colorchannelmixer=aa={strength},setpts=PTS-STARTPTS+{dur}/TB[glow]')
  filters.append(f'{video}[glow]overlay=x=\'-100+(t-{dur})*160/{tail}\':y=0:shortest=0:eof_action=pass[vglow]');video='[vglow]'
 if visual_index is not None:
  mode=s.get('tailBlendMode','bright')
  if mode not in {'bright','screen','lighten','addition'}:mode='bright'
  opacity=bounded(s.get('tailBlendOpacity',40),40,0,70 if not visual_alpha else 100)/100
  visual_base=f'[{visual_index}:v]fps=25,scale=1080:1920:force_original_aspect_ratio=decrease,format=rgba,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black@0'
  if visual_alpha or mode=='bright':
   if not visual_alpha:
    cutoff=bounded(s.get('tailBlendCutoff',18),18,0,80)/100
    soft=bounded(s.get('tailBlendSoftness',12),12,1,50)/100
    visual_base+=f',lumakey=threshold={cutoff/2}:tolerance={max(.01,cutoff/2)}:softness={soft}'
   visual_base+=f',colorchannelmixer=aa={opacity},setpts=PTS-STARTPTS+{dur}/TB[custom]'
   filters.append(visual_base)
   filters.append(f'{video}[custom]overlay=0:0:shortest=0:eof_action=pass[vcustom]');video='[vcustom]'
  else:
   visual_base+=f',format=yuv420p,trim=duration={tail},tpad=start_mode=add:start_duration={dur}:color=black,trim=duration={dur+tail},setpts=PTS-STARTPTS[custom]'
   filters.append(visual_base)
   filters.append(f'{video}[custom]blend=all_mode={mode}:all_opacity={opacity}:shortest=0:eof_action=pass:repeatlast=0[vcustom]');video='[vcustom]'
 if tail and style in {'warmflare','flash'}:
  at=bounded(s.get('tailFlashStart',1),1,0,max(0,tail-.1))
  flashdur=bounded(s.get('tailFlashDuration',.3),.3,.1,min(1.5,tail-at))
  strength=bounded(s.get('tailFlashStrength',60),60,0,100)/100
  color='0xffe9c8' if style=='warmflare' else 'white'
  filters.append(f'color=c={color}:s=1080x1920:r=25:d={flashdur},format=rgba,fade=t=in:st=0:d={flashdur/2}:alpha=1,fade=t=out:st={flashdur/2}:d={flashdur/2}:alpha=1,colorchannelmixer=aa={strength},setpts=PTS-STARTPTS+{dur+at}/TB[vflash]')
  filters.append(f'{video}[vflash]overlay=0:0:shortest=0:eof_action=pass,format=yuv420p[vfx]');video='[vfx]'
 if include_cover:
  filters += ['[1:v]fps=25,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[vcover]','anullsrc=r=48000:cl=stereo,atrim=duration=0.8[asilence]',f'[vcover][asilence]{video}[aclip]concat=n=2:v=1:a=1[vout][aout]']
  vf,af='[vout]','[aout]'
 else:vf,af=video,'[aclip]'
 out.parent.mkdir(parents=True,exist_ok=True)
 tmp=out.with_name(f'.{out.stem}.{secrets.token_hex(4)}.part.mp4')
 cmd=['ffmpeg','-y','-nostdin','-hide_banner','-loglevel','error',*inputs,'-filter_complex',';'.join(filters),'-map',vf,'-map',af]
 output_options=['-pix_fmt','yuv420p','-r','25','-c:a','aac','-b:a','192k','-ac','2','-ar','48000','-movflags','+faststart',str(tmp)]
 try:
  candidates=video_encoder_candidates(s)
  for index,encoder in enumerate(candidates):
   try:
    run([*cmd,*encoder,*output_options])
    break
   except RuntimeError:
    tmp.unlink(missing_ok=True)
    if index==len(candidates)-1:raise
  os.replace(tmp,out)
 finally:tmp.unlink(missing_ok=True)
 return str(out)

def verify_sample_cover(video,cover_path):
 """Compare the decoded first frame with the actual numbered cover."""
 with tempfile.TemporaryDirectory() as directory:
  frame=Path(directory)/'first.png'
  run(['ffmpeg','-y','-nostdin','-loglevel','error','-ss','0.2','-i',str(video),'-frames:v','1',str(frame)],timeout=60)
  with Image.open(frame) as first, Image.open(cover_path) as expected:
   first=first.convert('RGB').resize((270,480))
   expected=expected.convert('RGB').resize((270,480))
   error=sum(ImageStat.Stat(ImageChops.difference(first,expected)).mean)/3
   if error>28:raise RuntimeError('样片首帧与编号封面不一致，请检查渲染设置。')

def start_job(kind,fn):
 with LOCK:
  if STATE['job'] and STATE['job']['status']=='running':raise ValueError('已有任务进行中。')
  job={'id':secrets.token_hex(6),'kind':kind,'status':'running','progress':0,'message':'准备中','result':None,'error':None}
  STATE['job']=job
 def progress(msg,pct):
  with LOCK:job.update(message=msg,progress=round(pct,1))
 def worker():
  try:
   result=fn(progress)
   with LOCK:job.update(status='done',result=result,progress=100,message='完成')
  except Exception as e:
   with LOCK:job.update(status='error',error=str(e),message='任务失败')
 threading.Thread(target=worker,daemon=True).start()
 return job

def safe_out(folder):
 p=Path(folder).expanduser().resolve()
 if p==Path('/'):raise ValueError('不能将根目录设为输出目录。')
 p.mkdir(parents=True,exist_ok=True)
 return p

def export_stamp_path(folder,episode):
 """Keep resumable export bookkeeping outside the user's delivery folder."""
 key=hashlib.sha256(os.fsencode(str(folder.resolve()))).hexdigest()
 return CACHE/'exports'/key/f'{episode}.json'

def export_stamp_matches_file(stamp,dest):
 if not dest.is_file() or not stamp.is_file():return False
 try:
  info=json.loads(stamp.read_text(encoding='utf-8'))
  stat=dest.stat()
  return info.get('size')==stat.st_size and info.get('mtimeNs')==stat.st_mtime_ns
 except (OSError,ValueError,TypeError):return False

def record_export_stamp(stamp,dest,signature):
 stamp.parent.mkdir(parents=True,exist_ok=True)
 stat=dest.stat()
 data={'signature':signature,'size':stat.st_size,'mtimeNs':stat.st_mtime_ns}
 fd,name=tempfile.mkstemp(prefix='export-',suffix='.tmp',dir=stamp.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as stream:
   json.dump(data,stream)
   stream.flush();os.fsync(stream.fileno())
  os.replace(name,stamp)
 finally:Path(name).unlink(missing_ok=True)

def export_episodes(rows,settings,out,progress):
 episodes=[int(row['episode']) for row in rows]
 if any(ep<1 for ep in episodes) or len(set(episodes))!=len(episodes):
  raise ValueError('导出集号必须为不重复的正整数。')
 # Pure numeric names could collide with unrelated source footage in this folder.
 for ep in episodes:
  dest=out/f'{ep}.mp4'
  if dest.exists() and not export_stamp_matches_file(export_stamp_path(out,ep),dest):
   raise ValueError(f'{dest.name} 已存在且不是本任务已记录的导出文件；请更换输出目录或先移走该文件。')
 srt_folder=None
 if settings.get('exportSrt',False):
  if not settings.get('srtFolder'):
   raise ValueError('请单独选择 SRT 导出目录。')
  srt_folder=safe_out(settings['srtFolder'])
  if srt_folder==out:raise ValueError('SRT 目录必须与 MP4 目录分开。')
  for ep in episodes:
   dest=srt_folder/f'{ep}.srt'
   if dest.exists() and not export_stamp_matches_file(export_stamp_path(srt_folder,ep),dest):
    raise ValueError(f'{dest.name} 已存在且不是本任务已记录的字幕文件；请更换 SRT 目录或先移走该文件。')
 done=[];assets=render_assets(settings);started=time.monotonic()
 for i,row in enumerate(rows):
  ep=episodes[i];dest=out/f'{ep}.mp4';stamp=export_stamp_path(out,ep)
  remaining=round((time.monotonic()-started)/i*(len(rows)-i)/60) if i else None
  estimate=f' · 粗略剩余 {remaining} 分钟' if remaining is not None else ''
  progress(f'视频 {i+1}/{len(rows)} · EP{ep}{estimate}',i/len(rows)*100)
  signature=render_signature(settings,[row],assets)
  valid_signatures={signature}
  # Keep resumable beta.2 renders when the user explicitly selects the
  # unchanged software quality mode; fast mode produces a different encode.
  if settings.get('encodingMode')=='quality':
   old_settings=dict(settings);old_settings.pop('encodingMode',None)
   valid_signatures.add(render_signature(old_settings,[row],assets))
  reused=False
  if export_stamp_matches_file(stamp,dest):
   try:
    if json.loads(stamp.read_text(encoding='utf-8')).get('signature') in valid_signatures and probe(dest)['duration']>row['duration']-.5:
     done.append(str(dest));reused=True
   except (OSError,ValueError,KeyError):pass
  if not reused:
   done.append(export_one(row,settings,dest))
   record_export_stamp(stamp,dest,signature)
  if srt_folder:
   progress(f'字幕 {i+1}/{len(rows)} · EP{ep}',(i+.7)/len(rows)*100)
   srt_dest=srt_folder/f'{ep}.srt';srt_stamp=export_stamp_path(srt_folder,ep)
   if not (export_stamp_matches_file(srt_stamp,srt_dest) and
           json.loads(srt_stamp.read_text(encoding='utf-8')).get('signature') in valid_signatures):
    f=STATE['files'][row['source']]
    events=text_clean.analyze_text(f['path'],settings,CACHE)
    speech=text_clean.voice_segments(f['path'],CACHE,settings.get('language','en')) if f.get('audio',False) else []
    cues=text_clean.subtitle_cues(events,speech,row,.8 if settings.get('coverEnabled',True) else 0)
    temp=srt_dest.with_name(f'.{ep}.{secrets.token_hex(4)}.tmp')
    try:
     temp.write_text(text_clean.format_srt(cues),encoding='utf-8')
     os.replace(temp,srt_dest)
    finally:temp.unlink(missing_ok=True)
    record_export_stamp(srt_stamp,srt_dest,signature)
  progress(f'已完成 {i+1}/{len(rows)} · EP{ep}',(i+1)/len(rows)*100)
 return {'folder':str(out),'files':len(done),'srtFolder':str(srt_folder) if srt_folder else None}

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def headers_json(self,code=200,ctype='application/json'):
  self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store')
  self.send_header('Access-Control-Allow-Origin',self.headers.get('Origin','null'))
  self.send_header('Vary','Origin');self.send_header('Access-Control-Allow-Private-Network','true')
  self.send_header('Access-Control-Allow-Headers','Content-Type,X-Studio-Token')
  self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS');self.end_headers()
 def allowed_origin(self):
  origin=self.headers.get('Origin','')
  return not origin or origin=='null' or bool(re.match(r'^https://[a-z0-9-]+\.[a-z0-9-]+\.chatgpt\.site$',origin)) or origin.startswith('http://localhost:') or origin.startswith('http://127.0.0.1:')
 def auth(self):
  if not self.allowed_origin():raise PermissionError('网页来源未获授权')
  path=urllib.parse.urlparse(self.path).path
  query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
  provided=self.headers.get('X-Studio-Token','')
  if path in {'/video','/tail-video','/poster','/sample-cover'}:provided=provided or query.get('token',[''])[0]
  if not hmac.compare_digest(provided,TOKEN):raise PermissionError('连接码错误')
 def respond(self,data,code=200):
  raw=json.dumps(data,ensure_ascii=False,default=str).encode();self.headers_json(code);self.wfile.write(raw)
 def do_OPTIONS(self):
  if not self.allowed_origin():return self.respond({'error':'网页来源未获授权'},403)
  self.headers_json(204)
 def do_GET(self):
  try:
   self.auth(); path=urllib.parse.urlparse(self.path).path
   if path=='/state':
    with LOCK:self.respond({k:v for k,v in STATE.items() if k!='analyses'})
   elif path=='/job':self.respond(STATE['job'] or {})
   elif path=='/poster':
    p=STATE.get('poster')
    if not p or not Path(p).is_file():raise ValueError('未选择海报')
    raw=Path(p).read_bytes();self.headers_json(200,'image/jpeg' if Path(p).suffix.lower() in {'.jpg','.jpeg'} else 'image/png' if Path(p).suffix.lower()=='.png' else 'image/webp');self.wfile.write(raw)
   elif path=='/sample-cover':
    query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);n=int(query.get('episode',['-1'])[0])
    p=STATE['sampleCovers'].get(n)
    if not p or not Path(p).is_file():raise ValueError('样片封面不存在')
    self.headers_json(200,'image/png');self.wfile.write(Path(p).read_bytes())
   elif path in {'/video','/tail-video'}:
    query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);n=int(query.get('episode',['-1'])[0])
    p=STATE['samples'].get(n) if path=='/video' else STATE['tailPreviews'].get(n)
    if not p or not Path(p).is_file():raise ValueError('样片不存在')
    size=os.path.getsize(p);a=0;b=size-1;rang=self.headers.get('Range')
    if rang:
     m=re.match(r'bytes=(\d*)-(\d*)',rang)
     if m:a=int(m.group(1) or 0);b=min(size-1,int(m.group(2) or size-1))
    if a> b:raise ValueError('无效的播放范围')
    self.send_response(206 if rang else 200);self.send_header('Content-Type','video/mp4');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(b-a+1));self.send_header('Access-Control-Allow-Origin',self.headers.get('Origin','null'))
    if rang:self.send_header('Content-Range',f'bytes {a}-{b}/{size}')
    self.end_headers()
    with open(p,'rb') as f:
     f.seek(a);remaining=b-a+1
     while remaining:
      block=f.read(min(1024*1024,remaining))
      if not block:break
      self.wfile.write(block);remaining-=len(block)
   else:self.respond({'error':'未知路径'},404)
  except Exception as e:self.respond({'error':str(e)},403 if isinstance(e,PermissionError) else 400)
 def do_POST(self):
  try:
   self.auth(); n=int(self.headers.get('Content-Length',0))
   if n>2_000_000:raise ValueError('请求过大')
   data=json.loads(self.rfile.read(n) or b'{}');path=urllib.parse.urlparse(self.path).path
   if path=='/pick':
    import tkinter as tk
    from tkinter import filedialog
    root=tk.Tk();root.withdraw();root.attributes('-topmost',True)
    kind=data.get('kind','folder')
    result=filedialog.askdirectory() if kind=='folder' else filedialog.askopenfilename(filetypes=[('海报','*.png *.jpg *.jpeg *.webp')]);root.destroy()
    self.respond({'path':result})
   elif path=='/scan':self.respond(scan(data.get('folder',''),data.get('poster','')))
   elif path=='/source-order':
    paths=data.get('paths',[]);trims=data.get('tailTrims',{});head_trims=data.get('headTrims',{});mode=data.get('mode','manual')
    with LOCK:
     existing={f['path']:f for f in STATE['files']}
     if not isinstance(paths,list) or not paths or len(paths)!=len(set(paths)) or any(p not in existing for p in paths):raise ValueError('原集顺序无效，请重新导入。')
     ordered=[]
     for i,p in enumerate(paths):
      f=dict(existing[p]);f['sourceOrdinal']=i+1
      f['tailTrim']=bounded(trims.get(p,f.get('tailTrim',0)),0,0,30)
      f['headTrimFrames']=int(bounded(head_trims.get(p,f.get('headTrimFrames',0)),0,0,300))
      if f['duration']-f['tailTrim']<4:raise ValueError(f'{f["name"]} 的片尾裁切过长。')
      ordered.append(f)
     if mode=='auto':ordered.sort(key=lambda f:sortkey(Path(f['name'])))
     for i,f in enumerate(ordered):f['sourceOrdinal']=i+1
     STATE.update(files=ordered,plan=[],planParams=None,samples={},sampleCovers={},sampleSignature=None,orderConfirmed=True,sourceOrderMode='auto' if mode=='auto' else 'manual')
    persist()
    self.respond({'files':ordered,'duration':sum(f['duration'] for f in ordered),'size':sum(f['size'] for f in ordered)})
   elif path=='/set-poster':
    poster_path=checked_poster(data.get('poster',''))
    with LOCK:STATE['poster']=poster_path;STATE['samples']={};STATE['sampleCovers']={};STATE['sampleSignature']=None
    persist()
    self.respond({'poster':poster_path})
   elif path=='/validate-effect-audio':
    self.respond({'path':checked_effect_audio(data.get('path',''))})
   elif path=='/validate-effect-visual':
    self.respond({'path':checked_effect_visual(data.get('path',''))})
   elif path=='/plan':self.respond(start_job('plan',lambda progress:plan(data,progress)))
   elif path=='/update-cut':
    ix=int(data['index']);t=float(data['time'])
    with LOCK:
     rows=STATE['plan'];a=rows[ix];b=rows[ix+1] if ix+1<len(rows) else None
     adjacent=bool(b and a['source']==b['source'])
     upper=b['end']-3 if adjacent else STATE['files'][a['source']]['duration']
     if t<=a['start']+3 or t>=upper:raise ValueError('切点需在原集时长内；相邻片段两侧至少保留 3 秒。')
     a['end']=t;a['duration']=round(t-a['start'],2);a['cutReason']='人工调整';a['confidence']=100;a['review']=False
     if adjacent:b['start']=t;b['duration']=round(b['end']-t,2)
     STATE['samples']={};STATE['sampleCovers']={};STATE['sampleSignature']=None
    persist()
    self.respond({'plan':rows})
   elif path=='/sample':
    with LOCK:STATE['settings'].update(data.get('settings',{}))
    if STATE.get('planParams')!=plan_key(STATE['settings']):raise ValueError('分集设定已更改，请重新分析剧情并生成切点。')
    persist()
    ep=int(data['episode']);row=next((r for r in STATE['plan'] if r['episode']==ep),None)
    if not row:raise ValueError('找不到该集')
    def task(progress):
     progress(f'正在渲染 EP{ep} 样片',15)
     out=CACHE/'samples'/f'EP{ep:03}.mp4';result=export_one(row,STATE['settings'],out)
     numbered=CACHE/'render'/f'cover-{ep}.png' if STATE['settings'].get('coverEnabled',True) else None
     if numbered:verify_sample_cover(out,numbered)
     STATE['samples'][ep]=result;STATE['sampleCovers'][ep]=str(numbered) if numbered else ''
     STATE['sampleSignature']=render_signature(STATE['settings']);progress('样片可播放',100)
     cues=[]
     if STATE['settings'].get('exportSrt',False):
      f=STATE['files'][row['source']]
      events=text_clean.analyze_text(f['path'],STATE['settings'],CACHE)
      speech=text_clean.voice_segments(f['path'],CACHE,STATE['settings'].get('language','en')) if f.get('audio',False) else []
      cues=text_clean.subtitle_cues(events,speech,row,.8 if STATE['settings'].get('coverEnabled',True) else 0)
     return {'episode':ep,'url':f'/video?episode={ep}','coverUrl':f'/sample-cover?episode={ep}' if numbered else '', 'cues':cues}
    self.respond(start_job('sample',task))
   elif path=='/tail-preview':
    with LOCK:STATE['settings'].update(data.get('settings',{}))
    if STATE.get('planParams')!=plan_key(STATE['settings']):raise ValueError('分集设定已更改，请重新生成切点。')
    persist()
    ep=int(data['episode']);row=next((r for r in STATE['plan'] if r['episode']==ep),None)
    if not row:raise ValueError('找不到该集')
    def task(progress):
     progress(f'正在预览 EP{ep} 片尾',15)
     settings=dict(STATE['settings']);settings.update(coverEnabled=False,tailEnabled=True)
     preview=dict(row,start=max(float(row['start']),float(row['end'])-1.2))
     out=CACHE/'previews'/f'EP{ep:03}-tail.mp4'
     result=export_one(preview,settings,out)
     STATE['tailPreviews'][ep]=result;progress('片尾预览可播放',100)
     return {'episode':ep,'url':f'/tail-video?episode={ep}'}
    self.respond(start_job('tail',task))
   elif path=='/export':
    with LOCK:STATE['settings'].update(data.get('settings',{}))
    if STATE.get('planParams')!=plan_key(STATE['settings']):raise ValueError('分集设定已更改，请重新分析剧情并生成切点。')
    persist()
    out=safe_out(data['folder']);rows=list(STATE['plan']);settings=dict(STATE['settings'])
    if not rows:raise ValueError('请先生成拆集方案')
    if STATE.get('sampleSignature')!=render_signature(settings,rows):raise ValueError('封面、切点、源文件或片尾设置已变化，请重新生成样片。')
    if shutil.disk_usage(out).free < sum(f['size'] for f in STATE['files'])*.8:raise ValueError('输出磁盘空间不足，请更换目录或清理空间。')
    self.respond(start_job('export',lambda progress:export_episodes(rows,settings,out,progress)))
   else:self.respond({'error':'未知路径'},404)
  except Exception as e:self.respond({'error':str(e)},403 if isinstance(e,PermissionError) else 400)

def main():
 if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):sys.exit('请先安装 FFmpeg 和 ffprobe。')
 restore()
 parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
 print(f'\n拆集工作台本机助手已启动。\n连接地址：http://127.0.0.1:{args.port}\n连接码：{TOKEN}\n保持此窗口打开；关闭窗口会停止本机处理。\n',flush=True)
 ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
if __name__=='__main__':main()

