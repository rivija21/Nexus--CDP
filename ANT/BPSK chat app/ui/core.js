"use strict";
(function(){

// ------------------------------------------------------------------ helpers
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
const set = (id, text) => { const e = $(id); if(e) e.textContent = text; };
const cssv = k => (getComputedStyle(document.documentElement)
  .getPropertyValue(k).trim() || '120,120,120');

function bytes(n){
  n = Number(n)||0;
  if(n < 1024) return n + ' B';
  if(n < 1048576) return (n/1024).toFixed(n < 10240 ? 1 : 0) + ' kB';
  return (n/1048576).toFixed(2) + ' MB';
}
function rate(bps){
  bps = Number(bps)||0;
  if(bps < 1000) return bps.toFixed(0) + ' b/s';
  if(bps < 1e6) return (bps/1000).toFixed(bps < 10000 ? 1 : 0) + ' kb/s';
  return (bps/1e6).toFixed(2) + ' Mb/s';
}
function clock(ts){
  const d = new Date((Number(ts)||Date.now()/1000)*1000);
  return d.toTimeString().slice(0,5);
}
function ext(name){
  const m = /\.([A-Za-z0-9]{1,5})$/.exec(name||'');
  return m ? m[1].toUpperCase().slice(0,4) : 'FILE';
}
function initials(nick){
  return String(nick||'?').trim().split(/\s+/).slice(0,2)
    .map(w => w[0] ? w[0].toUpperCase() : '').join('') || '?';
}
const isImage = f => /^image\//.test((f && f.mime) || '');

// ------------------------------------------------------------------- state
const S = {
  nick:'me', peerNick:'peer', myAddr:'-', peerAddr:'-', peerUp:false,
  demo:false, connected:false, rows:new Map(), stats:{},
  tp:[], snr:[], stick:true
};

const stream = $('stream'), col = $('col'), logBox = $('log');

// --------------------------------------------------------------- transcript
function atBottom(){
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60;
}
function scrollDown(force){
  if(force || S.stick){
    stream.scrollTop = stream.scrollHeight;
    if($('jump')) $('jump').classList.remove('show');
  } else if($('jump')) $('jump').classList.add('show');
}
stream.addEventListener('scroll', () => {
  S.stick = atBottom();
  if(S.stick && $('jump')) $('jump').classList.remove('show');
});
if($('jump')) $('jump').onclick = () => { S.stick = true; scrollDown(true); };

const STATE = {
  queued:   {icon:'&#9675;',          cls:'pending', title:'queued for transmission'},
  sent:     {icon:'&#10003;',         cls:'pending', title:'transmitted, waiting for the acknowledgement'},
  sending:  {icon:'&#8942;',          cls:'pending', title:'transmitting'},
  delivered:{icon:'&#10003;&#10003;', cls:'ok',      title:'acknowledged by the peer'},
  received: {icon:'&#8595;',          cls:'ok',      title:'received'},
  receiving:{icon:'&#8942;',          cls:'pending', title:'receiving'},
  failed:   {icon:'&#9888;',          cls:'bad',     title:'no acknowledgement after every retry'},
  corrupt:  {icon:'&#9888;',          cls:'bad',     title:'reassembled but the CRC did not match'}
};

// The static half of a bubble. Rebuilt only when the author, the text or the
// attachment itself changes - rewriting it on every fragment would restart the
// image download several times a second.
function headHTML(m){
  const out = m.dir === 'out';
  let h = '';
  if(m.kind === 'system') return '<div class="text">' + esc(m.text) + '</div>';
  h += '<div class="who-line"><span class="n">' +
       esc(out ? (m.nick || S.nick) : (m.nick || 'peer')) +
       '</span><span class="a">addr ' + esc(m.addr) + '</span></div>';
  if(m.kind === 'text'){
    h += '<div class="text">' + esc(m.text) + '</div>';
    return h;
  }
  const f = m.file || {};
  if(isImage(f) && f.url){
    h += '<figure class="shot" data-full="' + esc(f.url) + '">' +
           '<div class="shot-img"><img src="' + esc(f.url) + '" alt="' +
             esc(f.name) + '" loading="lazy"></div>' +
           '<figcaption><span class="fn">' + esc(f.name) + '</span>' +
             '<span class="fs">' + bytes(f.size) + '</span>' +
             '<a class="dl" href="' + esc(f.url) + '" download title="save">' +
               '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
               'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
               'stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4"/>' +
               '<path d="M5 19h14"/></svg></a>' +
           '</figcaption></figure>';
    return h;
  }
  if(isImage(f)){
    // Still arriving: a framed placeholder rather than a broken image.
    h += '<figure class="shot pending-shot"><div class="shot-img skel">' +
         '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" ' +
         'stroke="currentColor" stroke-width="1.6" stroke-linecap="round">' +
         '<rect x="3" y="4" width="18" height="16" rx="3"/>' +
         '<circle cx="8.5" cy="9.5" r="1.6"/><path d="m4 17 5-5 4 4 3-2 4 4"/>' +
         '</svg></div><figcaption><span class="fn">' + esc(f.name) +
         '</span><span class="fs">' + bytes(f.size) + '</span></figcaption>' +
         '</figure>';
    return h;
  }
  h += '<div class="filecard"><span class="chip">' + esc(ext(f.name)) +
       '</span><span class="fmeta"><span class="fn">' + esc(f.name) +
       '</span><span class="fs">' + bytes(f.size) +
       (f.url ? ' &middot; <a href="' + esc(f.url) + '" download>save</a>' : '') +
       '</span></span></div>';
  return h;
}

function headSig(m){
  const f = m.file || {};
  return [m.kind, m.dir, m.nick, m.addr, m.text, f.name, f.mime, f.url, f.size]
    .join(' ');
}

// The live half: transfer progress, the current note, and the receipt line.
function tailHTML(m){
  let h = '';
  if(m.kind === 'file'){
    const p = m.progress || {};
    const busy = m.state === 'sending' || m.state === 'receiving'
                 || m.state === 'queued';
    if(p.total && busy){
      const pct = clamp(100 * (p.done||0) / p.total, 0, 100);
      h += '<div class="prog"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
           '<div class="progtxt"><span>' + (p.done||0) + ' / ' + p.total +
           ' fragments</span><span>' + pct.toFixed(0) + '%</span></div>';
    }
  }
  if(m.note) h += '<div class="note">' + esc(m.note) + '</div>';
  if(m.kind === 'system') return h;

  const st = STATE[m.state] || {icon:'', cls:'pending', title:m.state||''};
  h += '<div class="meta">';
  if(m.frames > 1) h += '<span class="badge">' + m.frames + ' frames</span>';
  if(m.tries > 1) h += '<span class="badge warn">retry &times;' + (m.tries-1) + '</span>';
  if(m.crc) h += '<span class="badge ' + (m.state === 'corrupt' ? 'bad' : 'ok') +
                 '">CRC ' + esc(m.crc) + '</span>';
  h += '<span class="time">' + clock(m.ts) + '</span>';
  h += '<span class="st ' + st.cls + '" title="' + esc(st.title) + '">' +
       st.icon + '</span></div>';
  return h;
}

function avatarHTML(m){
  if(m.kind === 'system') return '';
  return '<div class="avatar ' + (m.dir === 'out' ? 'me' : 'them') + '">' +
         esc(initials(m.dir === 'out' ? (m.nick || S.nick) : m.nick)) + '</div>';
}

function render(m){
  const key = String(m.dir) + ':' + String(m.id);
  let row = S.rows.get(key);
  const wasBottom = atBottom();
  if(!row){
    row = document.createElement('div');
    row.className = 'row ' + (m.kind === 'system'
      ? 'sys ' + (m.level||'info') : (m.dir === 'out' ? 'out' : 'in'));
    row.innerHTML = avatarHTML(m) +
      '<div class="bubble"><div class="head"></div><div class="tail"></div></div>';
    col.appendChild(row);
    S.rows.set(key, row);
  }
  const sig = headSig(m);
  if(row.dataset.sig !== sig){
    row.dataset.sig = sig;
    row.querySelector('.head').innerHTML = headHTML(m);
  }
  row.querySelector('.tail').innerHTML = tailHTML(m);
  // An image only takes up space once it has decoded, so a transcript that was
  // scrolled to the bottom would drift up under it.
  row.querySelectorAll('.shot img').forEach(img => {
    if(!img.complete) img.addEventListener('load', () => scrollDown(false), {once:true});
  });
  S.stick = wasBottom;
  scrollDown(false);
}

function addLog(line, ts){
  if(!logBox) return;
  const d = document.createElement('div');
  d.innerHTML = '<span class="t">' + clock(ts) + '</span>' + esc(line);
  logBox.appendChild(d);
  while(logBox.children.length > 300) logBox.removeChild(logBox.firstChild);
  logBox.scrollTop = logBox.scrollHeight;
}

// ----------------------------------------------------------------- lightbox
const lb = document.createElement('div');
lb.className = 'lightbox';
lb.innerHTML = '<img alt="attachment"><button class="lbx" aria-label="close">&times;</button>';
document.body.appendChild(lb);
const closeLb = () => lb.classList.remove('show');
lb.addEventListener('click', closeLb);
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeLb(); });
document.addEventListener('click', e => {
  const fig = e.target.closest && e.target.closest('.shot[data-full]');
  if(!fig || (e.target.closest && e.target.closest('.dl'))) return;
  lb.querySelector('img').src = fig.dataset.full;
  lb.classList.add('show');
});

// ---------------------------------------------------------------- telemetry
function tile(id, value, unit, cls){
  const el = $(id); if(!el) return;
  const v = el.querySelector('.v'); if(!v) return;
  v.innerHTML = value + (unit ? '<small>' + unit + '</small>' : '');
  el.classList.remove('good','warn','bad');
  if(cls) el.classList.add(cls);
}

function applyStats(s){
  S.stats = s;
  if(s.nick) S.nick = s.nick;
  if(s.peer_nick) S.peerNick = s.peer_nick;
  setPeer(s.peer_up, s.peer_nick, s.my_addr, s.peer_addr);

  const r = s.radio || {}, snr = r.snr_db;
  tile('tSnr', snr == null ? '--' : snr.toFixed(1), 'dB',
       snr == null ? null : snr > 12 ? 'good' : snr > 6 ? 'warn' : 'bad');
  tile('tLvl', r.level_db == null ? '--' : r.level_db.toFixed(0), 'dB');
  tile('tTx', (s.tx_bps/1000).toFixed(1), 'kb/s');
  tile('tRx', (s.rx_bps/1000).toFixed(1), 'kb/s');
  const per = Number(s.per)||0;
  tile('tPer', per.toFixed(1), '%', per < 2 ? 'good' : per < 10 ? 'warn' : 'bad');
  tile('tQ', (1000*(s.backlog||0)).toFixed(0), 'ms');

  set('cTxF', s.tx_frames||0);
  set('cRxOk', s.rx_valid||0);
  set('cRetx', s.retx||0);
  set('cRxBad', s.rx_bad||0);
  set('cAck', (s.ack_tx||0) + ' / ' + (s.ack_rx||0));
  set('cDup', s.rx_dup||0);
  set('cDrop', s.dropped||0);
  set('cFrag', s.frag_lost||0);
  set('kPer', per.toFixed(2) + ' %');
  set('kQ', s.queued||0);
  set('kBk', (1000*(s.backlog||0)).toFixed(0) + ' ms');
  set('kIdle', (s.idle_tx||0) + ' / ' + (s.idle_rx||0));
  set('kRounds', s.file_rounds||0);
  set('kPend', s.pending || 'idle');
  set('kBytes', bytes(s.tx_payload_bytes) + ' / ' + bytes(s.rx_payload_bytes));
  set('kUp', Math.floor(s.uptime||0) + ' s');
  set('kRev', s.revision || '--');
  set('thNow', rate(s.tx_bps) + ' / ' + rate(s.rx_bps));
  set('snrNow', snr == null ? '-- dB' : snr.toFixed(1) + ' dB');
  if(s.tx_freq && s.rx_freq)
    set('subtitle', (s.tx_freq/1e6).toFixed(1) + ' TX · ' +
        (s.rx_freq/1e6).toFixed(1) + ' RX MHz · FDD · differential BPSK');
  if(s.frag_size)
    set('cap', 'frag ' + s.frag_size + ' B · ACK ' +
        (s.ack_timeout||0).toFixed(2) + ' s · ' + (s.max_retries||0) +
        ' retries · ' + ((s.sym_rate||0)/1000).toFixed(0) + ' ksym/s');

  S.tp.push([s.tx_bps||0, s.rx_bps||0]); if(S.tp.length > 150) S.tp.shift();
  S.snr.push(snr == null ? null : snr);   if(S.snr.length > 150) S.snr.shift();
  drawSparks();
}

function setPeer(up, nick, mine, theirs){
  if(up != null) S.peerUp = !!up;
  const led = $('led');
  if(led) led.className = 'led' + (S.peerUp ? ' up' : '');
  set('peerName', nick || S.peerNick);
  set('peerState', S.peerUp ? 'online' : 'no carrier');
  if(mine != null){ S.myAddr = mine; S.peerAddr = theirs; }
  set('addrs', S.myAddr + ' → ' + S.peerAddr);
  set('title', S.nick);
  const av = $('peerAvatar');
  if(av) av.textContent = initials(nick || S.peerNick);
}

function fitCanvas(c){
  const d = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if(!w || !h) return null;
  if(c.width !== w*d || c.height !== h*d){ c.width = w*d; c.height = h*d; }
  const ctx = c.getContext('2d');
  ctx.setTransform(d,0,0,d,0,0);
  ctx.clearRect(0,0,w,h);
  return [ctx,w,h];
}
function series(ctx, w, h, data, pick, rgb, max){
  const n = data.length; if(n < 2) return;
  ctx.beginPath();
  for(let i=0;i<n;i++){
    const v = pick(data[i]);
    const x = w*i/(n-1), y = h - 3 - (h-6)*clamp((v||0)/max,0,1);
    i ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
  }
  ctx.strokeStyle = 'rgb(' + rgb + ')';
  ctx.lineWidth = 1.7; ctx.lineJoin = 'round'; ctx.stroke();
  ctx.lineTo(w,h); ctx.lineTo(0,h); ctx.closePath();
  const g = ctx.createLinearGradient(0,0,0,h);
  g.addColorStop(0, 'rgba(' + rgb + ',.20)');
  g.addColorStop(1, 'rgba(' + rgb + ',0)');
  ctx.fillStyle = g; ctx.fill();
}
function gridlines(ctx,w,h){
  ctx.strokeStyle = 'rgb(' + cssv('--c-grid') + ')';
  ctx.lineWidth = 1;
  for(let i=1;i<3;i++){
    const y = Math.round(h*i/3)+.5;
    ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke();
  }
}
function drawSparks(){
  const a = $('sparkTp');
  if(a){
    const fit = fitCanvas(a);
    if(fit){
      const ctx = fit[0], w = fit[1], h = fit[2];
      gridlines(ctx,w,h);
      const peak = Math.max(1000, ...S.tp.map(p => Math.max(p[0],p[1])));
      series(ctx,w,h,S.tp,p=>p[0],cssv('--c-tx'),peak);
      series(ctx,w,h,S.tp,p=>p[1],cssv('--c-rx'),peak);
    }
  }
  const b = $('sparkSnr');
  if(b){
    const fit = fitCanvas(b);
    if(fit){
      const ctx = fit[0], w = fit[1], h = fit[2];
      gridlines(ctx,w,h);
      series(ctx,w,h,S.snr,v=>v,cssv('--c-snr'),30);
    }
  }
}
window.addEventListener('resize', drawSparks);

// ------------------------------------------------------------------- banner
function banner(kind, text){
  const b = $('banner');
  if(!b) return;
  if(!kind){ b.className = 'banner'; return; }
  b.className = 'banner show ' + kind;
  set('bannerText', text);
}

// ------------------------------------------------------------------- events
function handle(ev){
  switch(ev.type){
    case 'hello':    loadState(ev.state); break;
    case 'message':  render(ev.message); break;
    case 'stats':    applyStats(ev.stats); break;
    case 'log':      addLog(ev.line, ev.t); break;
    case 'peer':     setPeer(ev.up); break;
    case 'identity': S.nick = ev.nick; S.peerNick = ev.peer_nick;
                     setPeer(null, ev.peer_nick); break;
    case 'reset':    col.innerHTML=''; S.rows.clear(); break;
  }
}

function loadState(st){
  if(!st) return;
  S.nick = st.nick; S.peerNick = st.peer_nick;
  S.myAddr = st.my_addr; S.peerAddr = st.peer_addr;
  col.innerHTML=''; S.rows.clear();
  (st.messages||[]).forEach(render);
  (st.logs||[]).forEach(l => addLog(l[1], l[0]));
  if(st.stats) applyStats(st.stats);
  setPeer(st.stats && st.stats.peer_up, st.peer_nick, st.my_addr, st.peer_addr);
  scrollDown(true);
}

let es = null, sawHello = false;
function connect(){
  try{ es = new EventSource('/events'); }catch(e){ return startDemo(); }
  es.onopen = () => { S.connected = true; if(!S.demo) banner(null); };
  es.onmessage = e => {
    sawHello = true; S.connected = true; S.demo = false; banner(null);
    try{ handle(JSON.parse(e.data)); }catch(err){}
  };
  es.onerror = () => {
    S.connected = false;
    if(!sawHello) return;              // the demo watchdog takes over instead
    banner('lost', 'radio process not reachable - reconnecting...');
  };
}

// ------------------------------------------------------------------ sending
async function post(url, body, headers){
  const r = await fetch(url, {method:'POST', body: body, headers: headers||{}});
  return r.json().catch(() => ({}));
}
function sendText(){
  const box = $('input');
  const t = box.value.trim();
  if(!t) return;
  box.value=''; resize(); if($('send')) $('send').disabled = true;
  if(S.demo) return demoSend(t);
  post('/api/send', JSON.stringify({text:t}), {'Content-Type':'application/json'})
    .catch(() => addLog('send failed - is the flowgraph running?'));
}
function sendFiles(list){
  [...list].forEach(f => {
    if(S.demo) return demoFile(f);
    f.arrayBuffer().then(buf => post('/api/upload', buf, {
      'X-Filename': f.name, 'X-Filetype': f.type || 'application/octet-stream',
      'Content-Type': 'application/octet-stream'
    })).catch(() => addLog('upload failed'));
  });
}

const input = $('input');
function resize(){
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 150) + 'px';
}
input.addEventListener('input', () => {
  resize();
  if($('send')) $('send').disabled = !input.value.trim();
});
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendText(); }
});
if($('send')) $('send').onclick = sendText;
if($('attach')) $('attach').onclick = () => $('file').click();
if($('file')) $('file').onchange = e => { sendFiles(e.target.files); e.target.value=''; };
if($('togglePanel')) $('togglePanel').onclick = () => {
  const p = $('panel');
  const open = p.classList.toggle('open');
  $('togglePanel').classList.toggle('on', open);
  document.body.classList.toggle('panel-open', open);
  drawSparks();
};
document.addEventListener('paste', e => {
  const items = [...((e.clipboardData && e.clipboardData.files) || [])];
  if(items.length){ e.preventDefault(); sendFiles(items); }
});
['dragenter','dragover'].forEach(t => stream.addEventListener(t, e => {
  e.preventDefault(); stream.classList.add('drag');
}));
['dragleave','drop'].forEach(t => stream.addEventListener(t, e => {
  e.preventDefault();
  if(t === 'drop' && e.dataTransfer) sendFiles(e.dataTransfer.files);
  stream.classList.remove('drag');
}));

// ---------------------------------------------------------------- demo mode
// With no radio answering, the page still has to be worth looking at: it runs a
// self-contained mock so the layout can be reviewed and demonstrated with no
// hardware. The banner makes it unmistakable.
let demoId = 100;
function startDemo(){
  if(S.demo) return;
  S.demo = true;
  banner('demo', 'Demo mode - no radio process on this port. Start the flowgraph and reload to go live.');
  S.nick = 'Node A'; S.peerNick = 'Node B'; S.myAddr = 1; S.peerAddr = 2;
  setPeer(true, 'Node B', 1, 2);
  const t = Date.now()/1000;
  [
    {id:'d1',dir:'sys',kind:'system',level:'info',ts:t-240,
     text:'link up [r5-applayer] · my_addr=1 peer_addr=2 · frag 256 B · ACK 0.50 s · 5 retries'},
    {id:'d2',dir:'sys',kind:'system',level:'info',ts:t-236,text:'peer 2 reachable'},
    {id:'d3',dir:'in',kind:'text',nick:'Node B',addr:2,state:'received',ts:t-208,
     text:'Receiver is locked. Constellation is two tight blobs at 45 dB Rx gain.'},
    {id:'d4',dir:'out',kind:'text',nick:'Node A',addr:1,state:'delivered',tries:2,ts:t-190,
     text:'Copy. Dropping Tx attenuation to 20 dB and sending the sweep from the bench run.'},
    {id:'d5',dir:'out',kind:'file',nick:'Node A',addr:1,state:'delivered',ts:t-150,
     file:{name:'rx_spectrum_915MHz.png',mime:'image/png',size:41210,url:DEMO_IMAGE},
     progress:{done:162,total:162}},
    {id:'d6',dir:'in',kind:'text',nick:'Node B',addr:2,state:'received',ts:t-96,
     text:'Got it - CRC matched on the first pass, three fragments needed a repair round.'},
    {id:'d7',dir:'in',kind:'file',nick:'Node B',addr:2,state:'received',ts:t-60,
     crc:'2820d377',
     file:{name:'link_budget.csv',mime:'text/csv',size:2841,url:'#'},
     progress:{done:12,total:12}},
    {id:'d8',dir:'out',kind:'file',nick:'Node A',addr:1,state:'sending',tries:2,ts:t-8,
     note:'retransmitting fragment 76',
     file:{name:'constellation_20dB.png',mime:'image/png',size:25027,url:null},
     progress:{done:78,total:99}}
  ].forEach(render);
  [[t-150,'sending rx_spectrum_915MHz.png: 41210 bytes in 162 fragments'],
   [t-140,'peer is missing 3 fragments, resending them (round 2)'],
   [t-132,'peer confirmed rx_spectrum_915MHz.png received complete and CRC-correct'],
   [t-60,'file chat_files/rx_link_budget.csv written, 2841 bytes, CRC ok'],
   [t-8,'sending constellation_20dB.png: 25027 bytes in 99 fragments']
  ].forEach(l => addLog(l[1], l[0]));
  let k = 0;
  setInterval(() => {
    k++;
    const snr = 14.6 + 2.5*Math.sin(k/9) + (Math.random()-.5)*0.9;
    applyStats({
      nick:'Node A', peer_nick:'Node B', peer_up:true, my_addr:1, peer_addr:2,
      radio:{snr_db:snr, level_db:-34 + 2*Math.sin(k/13), locked:true},
      tx_bps: 26000 + 9000*Math.sin(k/7), rx_bps: 17000 + 7000*Math.cos(k/5),
      per: clamp(1.6 + 1.3*Math.sin(k/11), 0, 100),
      tx_frames: 1902+k*3, rx_valid: 1744+k*3, retx: 24+((k/6)|0),
      rx_bad: 31+((k/9)|0), ack_tx: 1731+k*3, ack_rx: 1802+k*3, rx_dup: 6,
      dropped: 1, frag_lost: 3, idle_tx: 902+k, idle_rx: 880+k, file_rounds: 2,
      queued: (k%7===0)?3:0, backlog: 0.021 + 0.012*Math.abs(Math.sin(k/4)),
      pending: (k%7===0)?'file-frag 78':null,
      tx_payload_bytes: 68200+k*400, rx_payload_bytes: 30600+k*260,
      uptime: 243+k, revision:'r5-applayer', frag_size:256, ack_timeout:0.5,
      max_retries:5, sym_rate:250000, tx_freq:905.2e6, rx_freq:910.2e6
    });
  }, 900);
}
function demoSend(text){
  const id = 'd' + (++demoId);
  const m = {id:id, dir:'out', kind:'text', nick:S.nick, addr:1, text:text,
             state:'queued', ts:Date.now()/1000};
  render(m);
  setTimeout(() => { m.state='sent'; render(m); }, 260);
  setTimeout(() => { m.state='delivered'; render(m); }, 950);
  setTimeout(() => render({id:'d'+(++demoId), dir:'in', kind:'text',
    nick:'Node B', addr:2, state:'received', ts:Date.now()/1000,
    text:'(demo peer) acknowledged - ' + text.slice(0,48)}), 1800);
}
function demoFile(f){
  const id = 'd' + (++demoId), url = URL.createObjectURL(f);
  const m = {id:id, dir:'out', kind:'file', nick:S.nick, addr:1, state:'sending',
             file:{name:f.name, mime:f.type, size:f.size, url:url},
             progress:{done:0, total:Math.max(1, Math.ceil(f.size/254))},
             ts:Date.now()/1000};
  render(m);
  const step = () => {
    m.progress.done = Math.min(m.progress.total,
                               m.progress.done + Math.ceil(m.progress.total/12));
    if(m.progress.done >= m.progress.total){ m.state='delivered'; render(m); }
    else { render(m); setTimeout(step, 240); }
  };
  setTimeout(step, 300);
}

// -------------------------------------------------------------------- start
connect();
setTimeout(() => { if(!sawHello) startDemo(); }, 1200);
resize(); drawSparks();
})();
