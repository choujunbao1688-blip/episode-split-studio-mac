'use strict';
const {app,BrowserWindow,dialog,ipcMain}=require('electron');
const path=require('path');
const fs=require('fs');
const net=require('net');
const {spawn}=require('child_process');
const selfTest=process.argv.includes('--self-test');
let server=null,window=null,serverConfig=null;

function freePort(){return new Promise((resolve,reject)=>{const s=net.createServer();s.once('error',reject);s.listen(0,'127.0.0.1',()=>{let port=s.address().port;s.close(()=>resolve(port))})})}
function binary(name){return app.isPackaged?path.join(process.resourcesPath,name):name==='episode-agent'?path.join(__dirname,'build','agent-dist',name):path.join(__dirname,'build',name)}
function startupError(message){
 if(selfTest){console.error(message);if(server)server.kill('SIGTERM');app.exit(1)}
 else{dialog.showErrorBox('拆集工作台无法启动',message);app.quit()}
}
async function smoke(token,port){
 let error;
 for(let i=0;i<20;i++){
  try{
   const res=await fetch(`http://127.0.0.1:${port}/state`,{headers:{'X-Studio-Token':token},signal:AbortSignal.timeout(1000)});
   if(!res.ok)throw new Error(`HTTP ${res.status}`);
   await res.json();
   console.log('Packaged engine health check passed');
   app.quit();
   return;
  }catch(e){error=e;await new Promise(resolve=>setTimeout(resolve,250))}
 }
 startupError(`打包后的处理引擎未通过本机启动检测：${error?.message||'未知错误'}`);
}
async function launch(){
 const agent=binary('episode-agent'),bin=binary('bin');
 if(!fs.existsSync(agent)||!fs.existsSync(path.join(bin,'ffmpeg'))||!fs.existsSync(path.join(bin,'ffprobe')))return startupError('应用内的视频处理组件不完整，请重新安装。');
 const port=await freePort();
 server=spawn(agent,['--port',String(port)],{env:{...process.env,PATH:bin+path.delimiter+(process.env.PATH||''),PYTHONUNBUFFERED:'1'},stdio:['ignore','pipe','pipe']});
 let output='',started=false;
 const timer=setTimeout(()=>{if(!started)startupError('视频处理组件启动超时。')},15000);
 server.stdout.on('data',chunk=>{
  output+=chunk.toString();const match=output.match(/连接码：([^\s]+)/);
  if(match&&!started){
   started=true;clearTimeout(timer);serverConfig={token:match[1],port};
   if(selfTest)smoke(match[1],port);else createWindow(match[1],port)
  }
 });
 server.stderr.on('data',chunk=>{process.stderr.write(chunk)});
 server.on('error',e=>{clearTimeout(timer);startupError(e.message)});
 server.on('exit',(code)=>{if(!app.isQuitting)startupError(`视频处理组件意外退出（${code}）。`)})
}
function createWindow(token,port){
 window=new BrowserWindow({width:1440,height:950,minWidth:980,minHeight:680,title:'拆集工作台',backgroundColor:'#0c1012',webPreferences:{preload:path.join(__dirname,'preload.js'),contextIsolation:true,nodeIntegration:false,sandbox:true}});
 window.loadFile(path.join(__dirname,'web','index.html'),{query:{token,port:String(port),desktop:'1'}});
 window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
 window.on('closed',()=>{window=null});
}
ipcMain.handle('choose-path',async (_event,kind)=>{
 if(!window)return '';
 const choice=kind==='poster'?await dialog.showOpenDialog(window,{title:'选择纯净海报',properties:['openFile'],filters:[{name:'图片',extensions:['png','jpg','jpeg','webp']}]}):await dialog.showOpenDialog(window,{title:kind==='output'?'选择成片导出目录':'选择剧集目录',properties:['openDirectory']});
 return choice.canceled?'':choice.filePaths[0];
});
app.on('before-quit',()=>{app.isQuitting=true;if(server)server.kill('SIGTERM')});
app.whenReady().then(launch).catch(e=>startupError(e.message));
app.on('window-all-closed',()=>{if(process.platform!=='darwin')app.quit()});
app.on('activate',()=>{if(BrowserWindow.getAllWindows().length===0&&serverConfig&&!selfTest)createWindow(serverConfig.token,serverConfig.port)});
