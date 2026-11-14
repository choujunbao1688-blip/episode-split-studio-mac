'use strict';
const fs=require('fs');const path=require('path');
const sourceFfmpeg=require('ffmpeg-static');
const sourceProbe=require('@ffprobe-installer/ffprobe').path;
const dir=path.join(__dirname,'..','build','bin');fs.mkdirSync(dir,{recursive:true});
for(const [source,name] of [[sourceFfmpeg,'ffmpeg'],[sourceProbe,'ffprobe']]){
 if(!source||!fs.existsSync(source))throw Error('缺少 '+name+' 的 macOS ARM64 二进制文件');
 const dest=path.join(dir,name);fs.copyFileSync(source,dest);fs.chmodSync(dest,0o755);
}
