'use strict';
const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('desktop',{pick:(kind)=>ipcRenderer.invoke('choose-path',kind)});
