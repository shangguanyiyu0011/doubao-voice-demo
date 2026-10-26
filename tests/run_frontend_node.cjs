/* Optional developer runner. App itself never requires Node. Runs actual inline app JS. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {webcrypto}=require('node:crypto');
const elements=new Map();
function element(id=''){
 if(elements.has(id))return elements.get(id);
 const el={id,hidden:false,textContent:'',innerHTML:'',value:'',dataset:{},style:{setProperty(){}},
 classList:{add(){},remove(){},toggle(){}},append(){},remove(){},focus(){},setAttribute(){},
 addEventListener(){},querySelector(){return element('text'+id);},querySelectorAll(){return[];},
 getClientRects(){return[{}];},scrollHeight:0,scrollTop:0,clientHeight:800};
 elements.set(id,el);return el;
}
const document={getElementById:element,querySelector:()=>null,querySelectorAll:()=>[],
 createElement:()=>element('generated'+Math.random()),activeElement:element('focus'),
 documentElement:{outerHTML:'<html><script id="embeddedData" type="application/json">{}</script></html>'},
 body:element('body'),addEventListener(){},title:''};
element('embeddedData').textContent='{}';
const timers=new Set();
function later(fn,ms,...args){const id=setTimeout(()=>{timers.delete(id);fn(...args);},ms);timers.add(id);return id;}
class AudioMock{constructor(){this.paused=true;this.currentTime=0;this.duration=6;}pause(){this.paused=true;}load(){}removeAttribute(){}play(){this.paused=false;queueMicrotask(()=>this.onplaying?.());return Promise.resolve();}}
const storage=new Map();
const context={console,document,URL,URLSearchParams,TextEncoder,TextDecoder,Blob,Response,DOMException,AbortController,
 crypto:webcrypto,Audio:AudioMock,performance,queueMicrotask,Uint8Array,Float32Array,
 location:{search:'?test=1',protocol:'http:',href:'http://127.0.0.1:8765/?test=1'},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 navigator:{},setTimeout:later,clearTimeout,setInterval,clearInterval,
 requestAnimationFrame:fn=>later(()=>fn(performance.now()),16),cancelAnimationFrame:clearTimeout,
 confirm:()=>true,innerWidth:1440,innerHeight:900,
 fetch:async()=>new Response(JSON.stringify({app:'voice-script-studio-local',token:'mock-local-token',configured:false,voice:'zh_female_vv_uranus_bigtts',profile:{api:'v3'}})),
 atob:s=>Buffer.from(s,'base64').toString('binary'),btoa:s=>Buffer.from(s,'binary').toString('base64'),
 addEventListener(){}};
context.window=context;context.globalThis=context;context.isSecureContext=true;
element('app').contentWindow=context;
vm.createContext(context);
const html=fs.readFileSync(path.join(__dirname,'../豆包虚拟问答系统.html'),'utf8').replace(/\r\n/g,'\n');
let source=html.slice(html.indexOf("<script>\n'use strict';")+9,html.lastIndexOf('</script>'));
source=source.replace(/boot\(\)\.catch\(e=>[\s\S]*$/, "ready=true;proxyInfo={token:'test-local'};");
vm.runInContext(source,context,{filename:'actual-app.js'});
vm.runInContext(fs.readFileSync(path.join(__dirname,'frontend.js'),'utf8'),context,{filename:'frontend.js'});
const started=Date.now();
const watch=setInterval(()=>{
 if(document.body.dataset.complete==='true'||Date.now()-started>20000){
  clearInterval(watch);console.log(element('results').textContent);
  for(const timer of timers)clearTimeout(timer);
  process.exit(document.title==='TESTS PASSED'?0:1);
 }
},50);
