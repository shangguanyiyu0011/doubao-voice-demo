'use strict';
(async()=>{
 const output=document.getElementById('results'),frame=document.getElementById('app');
 const pause=ms=>new Promise(r=>setTimeout(r,ms));
 const until=async fn=>{for(let n=0;n<400;n++){if(fn())return;await pause(10);}throw Error('等待状态超时');};
 const results=[];
 const assert=(value,message)=>{if(!value)throw Error(message);};
 const test=async(name,fn)=>{try{await fn();results.push('PASS '+name);}catch(e){results.push('FAIL '+name+': '+e.message);}output.textContent=results.join('\n');};
 try{
 await until(()=>frame.contentWindow.__appTest?.ready);
 const w=frame.contentWindow,a=w.__appTest;
 let resolvePlay,played=[],failTTS=false,delayedTTS=false,ttsStarted=false,maxInFlight=0,inFlight=0;
 const originalPlay=a.Player.playBlob.bind(a.Player);
 const originalFetch=w.fetch.bind(w);
 a.Player.unlock=async()=>{};
 a.Player.playBlob=(asset,text,c,onText,signal)=>{played.push(text);return new Promise((resolve,reject)=>{const cancel=()=>reject(new w.DOMException('cancelled','AbortError'));signal.addEventListener('abort',cancel,{once:true});a.Player.cancel=cancel;resolvePlay=()=>{signal.removeEventListener('abort',cancel);a.Player.cancel=null;onText(text);resolve();};});};
 a.cfg.responseDelay=0;a.cfg.cooldownMs=0;
 a.steps=[1,2,3].map(n=>({id:'t'+n,q:'Q0'+n,a:'A0'+n}));
 a.AudioStore.db=null;a.AudioStore.memory.clear();
 const blob=new w.Blob([new Uint8Array(100)],{type:'audio/mpeg'});
 async function seed(){for(const s of a.steps)await a.AudioStore.put({key:await a.ttsKey(s),blob,kind:'tts',timings:[]});}
 await seed();
 w.fetch=async(url,options={})=>{
  if(String(url).startsWith('/api/tts/')&&options.method==='POST'){
   if(String(url).endsWith('/cancel'))return new w.Response('{}');
   if(String(url).endsWith('/lookup')){
    ttsStarted=true;if(delayedTTS)await new Promise((res,rej)=>options.signal.addEventListener('abort',()=>rej(new w.DOMException('cancelled','AbortError')),{once:true}));
    if(failTTS)return new w.Response(JSON.stringify({error:{message:'模拟 TTS 失败'}}),{status:502});
    return new w.Response(JSON.stringify({cache_hit:false}));
   }
   // Batch test errors intentionally: never fall through to real paid API.
   inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);await pause(20);inFlight--;
   return new w.Response(JSON.stringify({error:{message:'模拟失败，测试继续下一项'}}),{status:502});
  }
  return originalFetch(url,options);
 };
 await test('新通话 currentRound=0',async()=>{await a.startCall({manual:true});assert(a.STATE.index===0,'没有归零');});
 for(let i=0;i<3;i++)await test(`第 ${i+1} 次任意提问严格播放 A0${i+1}，完整播放后才推进`,async()=>{
  const run=a.triggerTurn(['请直接回答第三题','完全无关的天气','重复第一题'][i]);await until(()=>a.STATE.phase==='speaking');
  assert(played.at(-1)==='A0'+(i+1),'回答乱序');assert(a.STATE.index===i,'播放前推进');
  await a.triggerTurn('重复事件');assert(a.messages.filter(m=>m.role==='user').length===i+1,'重复触发');
  resolvePlay();await run;assert(a.STATE.index===i+1,'播放完成后未推进');
 });
 await test('重播已完成本轮不推进',async()=>{const index=a.STATE.index,run=a.replay();await until(()=>a.STATE.phase==='speaking');resolvePlay();await run;assert(a.STATE.index===index,'重播推进');});
 await test('上一轮、跳过、重新开始人工定位',async()=>{await a.jumpTo(2);assert(a.STATE.index===2,'上一轮失败');await a.jumpTo(3);assert(a.STATE.index===3,'跳过失败');await a.jumpTo(0);assert(a.STATE.index===0,'重置失败');});
 await test('TTS失败保留当前轮；重试成功播完才推进',async()=>{
  a.AudioStore.memory.clear();failTTS=true;await a.triggerTurn('任意问题');assert(a.STATE.index===0&&a.STATE.phase==='error','失败推进');
 failTTS=false;await seed();const run=a.retryPending();await until(()=>a.STATE.phase==='speaking');assert(a.STATE.index===0,'重试提前推进');resolvePlay();await run;assert(a.STATE.index===1,'重试未推进');
 });
 await test('失败后不能通过重新监听解除轮次锁',async()=>{
  a.AudioStore.memory.clear();failTTS=true;await a.triggerTurn('失败');await a.beginListening();assert(a.STATE.roundLocked&&a.STATE.phase==='error','失败锁被解除');a.endCall();failTTS=false;await seed();await a.startCall({manual:true});
 });
 await test('播放期间识别关闭，挂断后不播放、不监听',async()=>{
  let stopped=0,aborted=0;const track={enabled:true,stop(){stopped++;}};
  a.STATE.media={getTracks:()=>[track],getAudioTracks:()=>[track]};
  a.STATE.recognition={abort(){aborted++;}};
  const run=a.triggerTurn('测试挂断');await until(()=>a.STATE.phase==='speaking');assert(a.STATE.recognition===null&&!track.enabled,'播放期间监听未关闭');
  a.endCall();await run;assert(!a.STATE.active&&a.STATE.index===0&&a.STATE.timer===null,'挂断未恢复');assert(stopped===1&&aborted===1&&a.STATE.recognition===null,'麦克风未释放');
 });
 await test('挂断取消 pending TTS，迟到响应不播放',async()=>{
  await a.startCall({manual:true});a.AudioStore.memory.clear();delayedTTS=true;ttsStarted=false;const count=played.length,run=a.triggerTurn('挂断生成');await until(()=>ttsStarted);a.endCall();await run;assert(played.length===count&&!a.STATE.active,'迟到播放');delayedTTS=false;
 });
 await test('SpeechRecognition result/end 重复事件不触发双轮，识别仅显示文字',async()=>{
  await seed();let instances=[];
  w.SpeechRecognition=class{constructor(){instances.push(this);}start(){}stop(){}abort(){}};
  await a.startCall({manual:true});a.STATE.media={getTracks:()=>[],getAudioTracks:()=>[]};await a.beginListening();const rec=instances.at(-1);
  rec.onresult({results:[[{transcript:'把第三条提前回答'}]]});rec.onresult({results:[[{transcript:'把第三条提前回答'}]]});
  const run=a.triggerTurn(null,'voice');await until(()=>a.STATE.phase==='speaking');assert(a.messages.at(-2).text==='把第三条提前回答','未显示识别文字');assert(played.at(-1)==='A01','识别改变顺序');assert(a.STATE.recognition===null,'未停止识别');resolvePlay();await run;assert(a.STATE.index===1,'重复推进');a.endCall();const n=instances.length;await pause(650);assert(instances.length===n,'挂断后重启识别');
 });
 await test('批量单项失败不中止，失败轮次可重试，并发不超过2',async()=>{
  a.steps=[1,2,3,4,5].map(n=>({id:'batch'+n,q:'问题'+n,a:'回答'+n}));a.AudioStore.memory.clear();a.openSettings('voice');await a.preloadAll();
  assert(a.progress.failed===5&&a.progress.errors.length===5,'失败中止批量');assert(maxInFlight<=2,'并发过高');
  await a.preloadAll(true);assert(a.progress.total===5,'失败项重试缺失');a.closeSettings(true);
 });
 await test('前端音频键随文本、音色、语速改变',async()=>{
  const s={a:'中文，标点！\n下一句。'},c={...a.cfg};const keys=await Promise.all([a.ttsKey(s,c),a.ttsKey({a:s.a+'改变'},c),a.ttsKey(s,{...c,voice:'zh_female_xiaohe_uranus_bigtts'}),a.ttsKey(s,{...c,speed:1.2})]);assert(new Set(keys).size===4,'缓存键相同');
 });
 await test('服务端返回有效字级时间戳时优先使用',async()=>{
  const originalAudio=a.Player.audio;let current=.5;const mock={paused:false,duration:100,get currentTime(){return current;},play(){queueMicrotask(()=>this.onplaying?.());return Promise.resolve();},pause(){this.paused=true;},load(){},removeAttribute(){}};
  a.Player.audio=mock;const ac=new w.AbortController();let visible='';const run=originalPlay({blob,kind:'tts',timings:[{start:0,end:1,from:0,to:2},{start:1,end:2,from:2,to:4}]},'你好世界',a.cfg,t=>visible=t,ac.signal);
  await pause(70);assert(visible==='你','没有优先使用真实时间戳');current=2;mock.onended();await run;a.Player.audio=originalAudio;
 });
 await test('原 UI 主区域、F8控制台、语音设置无密钥输入',async()=>{
  for(const id of ['app','chat','filesList','moreButton','startCall','callCard','settingsOverlay','controller','prevTurn','skipTurn','replayTurn','resetFlow'])assert(w.document.getElementById(id),'UI缺失 '+id);
  a.openSettings('voice');assert(!w.document.querySelector('[data-secret],#ttsApiKey'),'仍有密钥输入');assert(w.document.getElementById('f-voice'),'音色设置缺失');a.closeSettings(true);
 });
 await test('真实播放器根据音频时间更新字幕、结束后才完成',async()=>{
  // Deterministic Audio object drives the actual Player method and actual RAF subtitle code.
  const originalAudio=a.Player.audio;let current=0;
  const mock={paused:false,duration:6,volume:1,playbackRate:1,muted:false,get currentTime(){return current;},play(){this.paused=false;queueMicrotask(()=>this.onplaying?.());return Promise.resolve();},pause(){this.paused=true;},load(){},removeAttribute(){}};
  a.Player.audio=mock;const ac=new w.AbortController();let visible='',finished=false;
  const run=originalPlay({blob,kind:'tts',timings:[]},'你好，今天很高兴见到你。我们开始吧！',a.cfg,t=>visible=t,ac.signal).then(()=>finished=true);
  current=3;await pause(70);assert(visible.length>0&&visible.length<20,'字幕未按进度更新');assert(!finished,'提前完成');
  const before=visible;mock.pause();await pause(70);assert(before===visible,'停止后字幕继续');
  current=6;mock.onended();await run;assert(finished,'ended未完成');a.Player.audio=originalAudio;
 });
 await test('服务启动较晚时可以重新连接，不必刷新页面',async()=>{
  let online=false;
  w.fetch=async url=>{if(!online)throw new TypeError('offline');return new w.Response(JSON.stringify(String(url).endsWith('/bootstrap')?{app:'voice-script-studio-local',token:'fresh'}:{ok:true}));};
  try{await a.connectLocal();}catch{}
  online=true;assert((await a.localJSON('/api/tts/lookup',{})).ok,'未恢复连接');
 });
 await test('服务重启后自动更新失效的本地会话',async()=>{
  let posts=0,bootstrap=0;
  w.fetch=async url=>{if(String(url).endsWith('/bootstrap')){bootstrap++;return new w.Response(JSON.stringify({app:'voice-script-studio-local',token:'new'}));}posts++;return new w.Response(JSON.stringify(posts===1?{error:{code:'local_token'}}:{ok:true}),{status:posts===1?403:200});};
  assert((await a.localJSON('/api/tts/lookup',{})).ok&&posts===2&&bootstrap===1,'未更新会话');
 });
 a.endCall();w.fetch=originalFetch;
 }catch(e){results.push('FAIL harness: '+e.stack);}
 const failed=results.filter(x=>x.startsWith('FAIL')).length;
 output.textContent=results.join('\n')+`\n\nRESULT: ${results.length-failed} passed, ${failed} failed`;
 document.title=failed?'TESTS FAILED':'TESTS PASSED';document.body.dataset.complete='true';
})();
