#!/usr/bin/env python3
"""Episode Split Studio local processor. Python 3.10+, FFmpeg, Pillow."""
import argparse, array, base64, hashlib, hmac, json, math, os, re, secrets, shutil, subprocess, sys, threading, time, urllib.parse, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

VIDEO_EXT={'.mp4','.mov','.mkv','.m4v','.avi'}
STATE={'files':[], 'plan':[], 'settings':{}, 'job':None, 'samples':{}, 'warnings':[]}
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
 return {'duration':round(dur,3),'width':v.get('width'), 'height':v.get('height'), 'audio':any(x['codec_type']=='audio' for x in d['streams'])}

def sortkey(p):
 m=re.search(r'(?i)(?:EP|第)\s*0*(\d+)',p.stem)
 return (int(m.group(1)) if m else 999999,p.name.lower())

def scan(folder,poster):
 root=Path(folder).expanduser().resolve()
 if not root.is_dir(): raise ValueError('剧集目录不存在。NAS 请先在电脑上挂载，再输入挂载目录。')
 paths=sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT),key=sortkey)
 if not paths: raise ValueError('该目录中没有可读取的视频。')
 files=[]; warnings=[]
 for i,p in enumerate(paths):
  try: meta=probe(p); files.append({'path':str(p),'name':p.name,'size':p.stat().st_size,**meta})
  except Exception as e: warnings.append(f'{p.name}: {e}')
 if not files: raise ValueError('没有成功读取的视频。')
 numbers=[int(m.group(1)) for f in files if (m:=re.search(r'(?i)EP\s*0*(\d+)',f['name']))]
 if len(numbers)!=len(set(numbers)): warnings.append('存在重复的 EP 编号。')
 if numbers and len(numbers)==len(files) and max(numbers)-min(numbers)+1!=len(numbers): warnings.append('EP 编号存在缺集。')
 if poster:
  q=Path(poster).expanduser().resolve()
  if not q.is_file() or q.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}: raise ValueError('海报须为本机可读取的 PNG、JPG 或 WebP。')
  Image.open(q).verify()
 with LOCK: STATE.update(files=files,poster=str(q) if poster else '',plan=[],warnings=warnings)
 persist()
 return {'files':files,'warnings':warnings,'duration':sum(f['duration'] for f in files),'size':sum(f['size'] for f in files)}

def persist():
 with LOCK:data={k:STATE.get(k) for k in ('files','poster','plan','settings','warnings')}
 p=CACHE/'state.json';tmp=p.with_suffix('.tmp')
 tmp.write_text(json.dumps(data,ensure_ascii=False));os.replace(tmp,p)

def restore():
 try:
  d=json.loads((CACHE/'state.json').read_text())
  if all(Path(f['path']).exists() for f in d.get('files',[])):STATE.update(d)
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

def boundaries(cands,end,count,minlen,maxlen):
 if count<=1 or end<minlen*2: return []
 out=[]; left=0
 for k in range(1,count):
  remain=count-k; ideal=end*k/count
  eligible=[c for c in cands if c['time']-left>=minlen and end-c['time']>=remain*minlen and abs(c['time']-ideal)<=max(maxlen,ideal*.45)]
  if eligible:
   c=max(eligible,key=lambda x:x['score']-abs(x['time']-ideal)/maxlen*1.4)
   t=c['time']; reason=c['reason']; conf=min(99,round(45+c['score']*12))
  else: t=round(ideal,2); reason='按时长建议，需复核'; conf=30
  out.append({'time':t,'reason':reason,'confidence':conf,'locked':False});left=t
 return out

def plan(settings, progress):
 with LOCK: files=list(STATE['files'])
 if not files: raise ValueError('请先导入剧集。')
 minlen=max(10,float(settings.get('minSeconds',45)));maxlen=max(minlen,float(settings.get('maxSeconds',60)))
 target=max(len(files),int(settings.get('target',len(files)*2)))
 analyses=[]
 for i,f in enumerate(files):
  progress(f'分析 {i+1}/{len(files)} · {f["name"]}',(i+1)/(len(files)+1)*75)
  candidates,intro,end=analyse(f)
  if not settings.get('removeOld',True):intro=0;end=f['duration']
  intro=max(intro,float(settings.get('trimIntroSeconds',0)))
  if end-intro<4:end=f['duration'];intro=0
  analyses.append((candidates,intro,end))
 # Soft episode count: preserve narrative cuts before duration, target count last.
 counts=[max(1,round((end-intro)/((minlen+maxlen)/2))) for _,intro,end in analyses]
 # Bring total toward requested count only where duration can support the change.
 for _ in range(abs(target-sum(counts))):
  delta=1 if sum(counts)<target else -1
  choices=[i for i,(_,intro,end) in enumerate(analyses) if (counts[i]+delta)>=1 and (delta<0 or (end-intro)/(counts[i]+1)>=minlen*.7)]
  if not choices: break
  ix=max(choices,key=lambda i: ((analyses[i][2]-analyses[i][1])/(counts[i]+1) if delta>0 else -(analyses[i][2]-analyses[i][1])/max(1,counts[i]-1)))
  counts[ix]+=delta
 rows=[]; startnum=max(1,int(settings.get('startEpisode',1)))
 for i,(f,(cands,intro,end),count) in enumerate(zip(files,analyses,counts)):
  local=[dict(c,time=c['time']-intro) for c in cands if intro<c['time']<end]
  cuts=boundaries(local,end-intro,count,minlen,maxlen)
  points=[intro]+[x['time']+intro for x in cuts]+[end]
  for j in range(len(points)-1):
   rows.append({'id':len(rows)+1,'episode':startnum+len(rows),'source':i,'file':f['name'],'start':round(points[j],2),'end':round(points[j+1],2),'duration':round(points[j+1]-points[j],2),'cutReason':cuts[j]['reason'] if j<len(cuts) else '原集结尾','confidence':cuts[j]['confidence'] if j<len(cuts) else 90,'review':points[j+1]-points[j]<minlen or points[j+1]-points[j]>maxlen or (j<len(cuts) and cuts[j]['confidence']<40)})
 with LOCK: STATE.update(plan=rows,settings=settings,analyses=[{'candidates':c,'cleanStart':a,'cleanEnd':e} for c,a,e in analyses])
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
  px=int(w*float(s.get('positionX',.5))); py=int(h*float(s.get('positionY',.26)))
  pos=(int(px-tw/2),int(py-th/2-box[1]))
  shadow=int(s.get('shadowBlur',12)); dx=int(s.get('shadowX',14));dy=int(s.get('shadowY',4))
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

def export_one(row,s,out,job=None):
 f=STATE['files'][row['source']];start=float(row['start']);dur=float(row['end'])-start
 if dur<=0: raise ValueError('切点顺序错误。')
 work=CACHE/'render';work.mkdir(exist_ok=True)
 coverpath=work/f'cover-{row["episode"]}.png';poster=cover(coverpath,row['episode'],s)
 include_cover=bool(poster and s.get('coverEnabled',True));tail=2.8 if s.get('tailEnabled',True) else 0
 clip='fps=25,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p'
 if tail:clip+=f',tpad=stop_mode=clone:stop_duration={tail},fade=t=out:st={dur+2.4}:d=0.4:color=white'
 inputs=['-ss',str(start),'-t',str(dur),'-i',f['path']]
 if include_cover:inputs+=['-loop','1','-t','0.8','-i',str(coverpath)]
 rise=None
 if tail:
  rise=work/'rise.wav'
  if not rise.exists():make_rise(rise)
  inputs+=['-i',str(rise)]
 filters=[f'[0:v]{clip}[vclip]']
 if f['audio']:filters.append(f'[0:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={dur+tail},asetpts=PTS-STARTPTS[abase]')
 else:filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={dur+tail}[abase]')
 if tail:
  ri=2 if include_cover else 1
  delay=max(0,int((dur+tail-2.2)*1000))
  filters.append(f'[{ri}:a]aresample=48000,adelay={delay}|{delay},atrim=duration={dur+tail}[rise]')
  filters.append('[abase][rise]amix=inputs=2:duration=first:normalize=0[aclip]')
 else: filters.append('[abase]anull[aclip]')
 if include_cover:
  filters += ['[1:v]fps=25,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[vcover]','anullsrc=r=48000:cl=stereo,atrim=duration=0.8[asilence]','[vcover][asilence][vclip][aclip]concat=n=2:v=1:a=1[vout][aout]']
  vf,af='[vout]','[aout]'
 else:vf,af='[vclip]','[aclip]'
 out.parent.mkdir(parents=True,exist_ok=True)
 tmp=out.with_suffix('.part.mp4')
 cmd=['ffmpeg','-y','-nostdin','-hide_banner','-loglevel','error',*inputs,'-filter_complex',';'.join(filters),'-map',vf,'-map',af,'-c:v','libx264','-preset',s.get('preset','medium'),'-crf',str(s.get('crf',19)),'-pix_fmt','yuv420p','-r','25','-c:a','aac','-b:a','192k','-ac','2','-ar','48000','-movflags','+faststart',str(tmp)]
 run(cmd)
 os.replace(tmp,out)
 return str(out)

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
  if path in {'/video','/poster'}:provided=provided or query.get('token',[''])[0]
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
   elif path=='/video':
    query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);n=int(query.get('episode',['-1'])[0])
    p=STATE['samples'].get(n)
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
   elif path=='/plan':self.respond(start_job('plan',lambda progress:plan(data,progress)))
   elif path=='/update-cut':
    ix=int(data['index']);t=float(data['time'])
    with LOCK:
     rows=STATE['plan'];a=rows[ix];b=rows[ix+1]
     if a['source']!=b['source'] or t<=a['start']+3 or t>=b['end']-3:raise ValueError('切点须在同一原集内，且两侧至少保留 3 秒。')
     a['end']=t;a['duration']=round(t-a['start'],2);a['cutReason']='人工调整';a['confidence']=100;a['review']=False
     b['start']=t;b['duration']=round(b['end']-t,2)
    persist()
    self.respond({'plan':rows})
   elif path=='/sample':
    with LOCK:STATE['settings'].update(data.get('settings',{}))
    persist()
    ep=int(data['episode']);row=next((r for r in STATE['plan'] if r['episode']==ep),None)
    if not row:raise ValueError('找不到该集')
    def task(progress):
     progress(f'正在渲染 EP{ep} 样片',15)
     out=CACHE/'samples'/f'EP{ep:03}.mp4';result=export_one(row,STATE['settings'],out)
     STATE['samples'][ep]=result;progress('样片可播放',100);return {'episode':ep,'url':f'/video?episode={ep}'}
    self.respond(start_job('sample',task))
   elif path=='/export':
    with LOCK:STATE['settings'].update(data.get('settings',{}))
    persist()
    out=safe_out(data['folder']);rows=list(STATE['plan']);settings=dict(STATE['settings']);prefix=re.sub(r'[^\w\u4e00-\u9fff-]','',data.get('prefix','成片'))[:36] or '成片'
    if not rows:raise ValueError('请先生成拆集方案')
    if shutil.disk_usage(out).free < sum(f['size'] for f in STATE['files'])*.8:raise ValueError('输出磁盘空间不足，请更换目录或清理空间。')
    def task(progress):
     done=[]
     for i,row in enumerate(rows):
      progress(f'导出 {i+1}/{len(rows)} · EP{row["episode"]}',i/len(rows)*100)
      dest=out/f'{prefix}-EP{row["episode"]:02d}.mp4'
      fingerprint=hashlib.sha256(json.dumps([row,settings,STATE['files'][row['source']]['size']],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
      stamp=dest.with_suffix('.splitstamp')
      if dest.exists() and stamp.exists() and stamp.read_text()==fingerprint:
       try:
        if probe(dest)['duration']>row['duration']-.5:done.append(str(dest));continue
       except Exception:pass
      done.append(export_one(row,settings,dest));stamp.write_text(fingerprint)
     manifest=out/'拆集清单.json';manifest.write_text(json.dumps({'settings':settings,'episodes':rows,'files':done},ensure_ascii=False,indent=2))
     return {'folder':str(out),'files':len(done),'manifest':str(manifest)}
    self.respond(start_job('export',task))
   else:self.respond({'error':'未知路径'},404)
  except Exception as e:self.respond({'error':str(e)},403 if isinstance(e,PermissionError) else 400)

def main():
 if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):sys.exit('请先安装 FFmpeg 和 ffprobe。')
 restore()
 parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
 print(f'\n拆集工作台本机助手已启动。\n连接地址：http://127.0.0.1:{args.port}\n连接码：{TOKEN}\n保持此窗口打开；关闭窗口会停止本机处理。\n',flush=True)
 ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
if __name__=='__main__':main()
