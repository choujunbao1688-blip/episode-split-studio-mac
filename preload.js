'use strict';
const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('desktop',{
 pick:(kind)=>ipcRenderer.invoke('choose-path',kind),
 openHistory:(kind,mode,episode)=>ipcRenderer.invoke('open-history',kind,mode,episode)
});
