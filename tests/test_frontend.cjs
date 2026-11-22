'use strict';
// Run with node tests/test_frontend.cjs; deterministic DOM/WebSocket stubs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(id='') { this.id=id; this.handlers={}; this.value=''; this.textContent='';this.dataset={};this.checked=false;this.className='';this.style={};this.attrs={};this.children=[];this.classSet=new Set(); this.classList={add:n=>this.classSet.add(n),remove:n=>this.classSet.delete(n),contains:n=>this.classSet.has(n),toggle:(n,v)=>{if(v===undefined)v=!this.classSet.has(n);v?this.classSet.add(n):this.classSet.delete(n);return v}}; }
  addEventListener(name,fn){(this.handlers[name]??=[]).push(fn);}
  dispatch(name,evt={}) {for(const f of this.handlers[name]??[]) f({type:name,preventDefault(){},pointerId:1,clientX:100,clientY:100,...evt,target:this});}
  append(...nodes){this.children.push(...nodes);}
  appendChild(node){this.children.push(node);}
  replaceChildren(...nodes){this.children=[...nodes];}
  setAttribute(k,v){this.attrs[k]=v;}
  setPointerCapture(){};
  focus(){};select(){};click(){this.dispatch('click')};
}
async function main() {
  const ids='status pairPanel controlPanel pairMessage pairCode pairButton deviceName light leftClick rightClick dragButton pad scroll typeInput keyboardMessage sendButton clearText copyToPi pasteToPi readPiClipboard readPhoneClipboard copyToPhone sensitivity naturalScroll haptics speedValue reconnectButton installHelp installButton appsList appsMessage refreshApps appsSearch showHiddenApps homeFavorites homeStatus cpuTemp cpuUse memUse uptime restartPi shutdownPi powerHelp systemMessage quickMoonlight homeMouse'.split(' ');
  const elements=Object.fromEntries([...ids,...['home','mouse','keyboard','moonlight','apps','settings'].map(x=>'tab-'+x)].map(k=>[k,new Element(k)]));
  const tabs=['home','mouse','keyboard','moonlight','apps','settings'].map(x=>{const e=new Element();e.dataset.tab=x;return e});
  const keys=['enter','f11','escape'].map(x=>{const e=new Element();e.dataset.key=x;return e});
  const combos=['ctrl+v','alt+tab'].map(x=>{const e=new Element();e.dataset.combo=x;return e});
  const repeats=['up','down'].map(x=>{const e=new Element();e.dataset.repeatKey=x;return e});
  const connection=new Element();
  const storage={'pi_remote_device_token_v1':'a'.repeat(48)};
  const messages=[];
  let sockets=[];
  class Socket {
    static OPEN=1;static CONNECTING=0;static CLOSED=3;
    constructor(){this.readyState=0;sockets.push(this);setTimeout(()=>{this.readyState=1;this.onopen?.()},2)}
    send(s){const data=JSON.parse(s);messages.push(data);if(data.type==='auth')setTimeout(()=>this.onmessage?.({data:'{"type":"ready"}'}),2);if(data.type==='apps-list')setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'apps',apps:[{id:'moonlight',name:'Moonlight',description:'Game streaming on your Pi',available:true}]})}),2);if(data.type==='app-launch')setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'app-status',ok:true,id:data.id,message:'Launch requested'})}),2);if(data.type==='ping')setTimeout(()=>this.onmessage?.({data:'{"type":"pong"}'}),2);}
    close(){this.readyState=3;this.onclose?.()}
  }
  const doc = {
    hidden:false,
    listeners:{},
    getElementById:id=>elements[id]??new Element(),
    createElement:tag=>new Element(),
    querySelector:s=>s==='.connection'?connection:tabs.find(e=>`[data-tab="${e.dataset.tab}"]`===s),
    querySelectorAll:s=>s==='[data-tab]'?tabs:s==='[data-key]'?keys:s==='[data-combo]'?combos:s==='[data-repeat-key]'?repeats:[],
    addEventListener(n,f){this.listeners[n]=f}
  };
  const w={isSecureContext:false,listeners:{},addEventListener(n,f){this.listeners[n]=f},matchMedia:()=>({matches:false})};
  const context={console,document:doc,window:w,WebSocket:Socket,URLSearchParams,TextEncoder,AbortController,Date,Math,Number,JSON,performance,setTimeout,clearTimeout,setInterval,clearInterval,location:{hash:'',protocol:'http:',host:'steamlink.local:3100',pathname:'/',search:''},history:{replaceState(){}},navigator:{},localStorage:{getItem:k=>storage[k]??null,setItem:(k,v)=>{storage[k]=v},removeItem:k=>{delete storage[k]}},fetch:async()=>({status:200,ok:true,json:async()=>({})})};
  const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
  vm.runInNewContext(source,context);
  await new Promise(r=>setTimeout(r,30));
  assert.equal(elements.status.textContent,'Connected');
  await new Promise(r=>setTimeout(r,8));
  assert.equal(elements.appsList.children.length,1,'Moonlight tile should appear');
  assert.equal(elements.appsList.children[0].children[0].children[1].children[0].textContent,'Moonlight');
  elements.appsList.children[0].children[1].children[0].dispatch('click');
  assert(messages.some(m=>m.type==='app-launch'&&m.id==='moonlight'),'Apps tile should send launch request');
  assert.equal(elements.homeFavorites.children.length,1,'Moonlight pinned on Home');
  elements.appsList.children[0].children[1].children[1].dispatch('click');
  assert.equal(elements.homeFavorites.children.length,1,'Unpinned Home displays empty hint');
  assert.equal(JSON.parse(storage.pi_remote_apps_v3).favorites.length,0);
  elements.appsList.children[0].children[1].children[1].dispatch('click');
  assert.equal(JSON.parse(storage.pi_remote_apps_v3).favorites[0],'moonlight');
  await new Promise(r=>setTimeout(r,10));
  assert.equal(elements['tab-moonlight'].classList.contains('hidden'),false,'Moonlight launch opens remote');
  sockets.at(-1).onmessage({data:JSON.stringify({type:'system-info',data:{moonlightRunning:true,powerEnabled:false}})});
  sockets.at(-1).onmessage({data:JSON.stringify({type:'system-info',data:{moonlightRunning:false,powerEnabled:false}})});
  assert.equal(elements['tab-mouse'].classList.contains('hidden'),false,'Moonlight closing returns to touchpad');
  elements.appsSearch.value='not-a-real-app';elements.appsSearch.dispatch('input');
  assert.equal(elements.appsList.children[0].textContent.startsWith('No matching apps'),true);
  elements.appsSearch.value='';elements.appsSearch.dispatch('input');
  sockets.at(-1).onmessage({data:JSON.stringify({type:'system-info',data:{cpuTemp:54.1,cpuPercent:27,memoryPercent:40,uptime:3700,powerEnabled:false,moonlightRunning:false}})});
  assert.equal(elements.cpuTemp.textContent,'54.1°C');
  assert.equal(elements.restartPi.disabled,true);

  const e=(id,type,pointerId,x,y)=>elements[id].dispatch(type,{pointerId,clientX:x,clientY:y});
  e('pad','pointerdown',1,100,100);e('pad','pointermove',1,120,105);e('pad','pointerup',1,120,105);
  assert(messages.some(m=>m.type==='move'),'one-finger movement should send move');
  messages.length=0;
  e('pad','pointerdown',10,100,100);e('pad','pointerdown',11,120,100);e('pad','pointerup',10,100,100);e('pad','pointerup',11,120,100);
  assert.equal(messages.filter(m=>m.type==='click'&&m.button==='right').length,1,'two-finger tap right-click');
  messages.length=0;
  e('pad','pointerdown',20,100,100);e('pad','pointerdown',21,120,100);e('pad','pointermove',20,100,24);e('pad','pointermove',21,120,24);
  e('pad','pointerup',20,100,24);e('pad','pointerup',21,120,24);
  assert(messages.some(m=>m.type==='scroll'),'two-finger movement should scroll');
  assert(!messages.some(m=>m.type==='click'&&m.button==='right'),'scroll must not right-click');
  elements.sensitivity.value='2.5';elements.sensitivity.dispatch('input',{target:elements.sensitivity});
  assert.equal(JSON.parse(storage.pi_remote_prefs_v2).sensitivity,2.5);
  elements.typeInput.value='Hello';elements.copyToPi.dispatch('click');
  assert(messages.some(m=>m.type==='clipboard-set'&&m.value==='Hello'));
  combos[0].dispatch('click');
  assert(messages.some(m=>m.type==='combo'&&m.name==='v'));
  console.log('PASS: pairing, apps, favorites, search, system status, power opt-in, gestures, sensitivity, clipboard, keyboard');
  process.exit(0);
}
main().catch(err=>{console.error(err);process.exit(1)});
