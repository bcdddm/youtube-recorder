"""YouTube Recorder GUI · By Leoluchino  (P9, v0.2 §7)

Local-only Flask app: Channels / Queue / Reports / Settings(排班表).
Security: binds 127.0.0.1 only; per-session CSRF token on every POST;
vault file routes are canonical-path-checked against the vault root;
API keys go straight to the macOS Keychain via `security`, never to disk.
"""

from __future__ import annotations

import re
import secrets
import subprocess
from pathlib import Path

from flask import (Flask, abort, redirect, render_template_string, request,
                   send_file, url_for)
from markupsafe import escape

from . import BRANDING, __version__
from . import config as cfg_mod
from . import db as dbm
from . import state as st

def _load_or_make_csrf() -> str:
    """持久化 CSRF 令牌：跨进程重启保持不变，避免 App 更新/重启后
    webview 里已打开的旧页面提交表单时令牌失配（403 Forbidden）。"""
    try:
        from .paths import APP_SUPPORT
        f = APP_SUPPORT / ".csrf"
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
            if len(tok) >= 16:
                return tok
        tok = secrets.token_hex(16)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(tok, encoding="utf-8")
        try:
            import os as _os
            _os.chmod(f, 0o600)
        except OSError:
            pass
        return tok
    except Exception:
        return secrets.token_hex(16)  # 退化：仍可用，只是重启后失效


CSRF = _load_or_make_csrf()
app = Flask(__name__)

BASE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{{ title }} · YouTube Recorder</title>
<style>
 :root{--bg:#121215;--card:#1b1b20;--card2:#222228;--fg:#e9e9ec;--dim:#9a9aa3;
       --acc:#e0a458;--ok:#6bbf59;--bad:#d16060;--line:#2a2a31;--bord:#3a3a42;
       --navbg:rgba(27,27,32,.92);--code:#0e0e12;--scroll:#333339;
       --okbg:rgba(107,191,89,.14);--badbg:rgba(209,96,96,.14);
       --runbg:rgba(224,164,88,.13);--accsel:rgba(224,164,88,.35);
       --shadow:none;--acctext:#141414}
 body[data-theme=light]{--bg:#f7f6f3;--card:#ffffff;--card2:#f1f1ef;
       --fg:#37352f;--dim:#787774;--acc:#d9730d;--ok:#448361;--bad:#d44c47;
       --line:#e9e9e7;--bord:#d3d1cb;--navbg:rgba(255,255,255,.9);
       --code:#f1f1ef;--scroll:#d3d1cb;
       --okbg:rgba(68,131,97,.11);--badbg:rgba(212,76,71,.11);
       --runbg:rgba(217,115,13,.11);--accsel:rgba(217,115,13,.25);
       --shadow:0 1px 2px rgba(15,15,15,.04),0 3px 9px rgba(15,15,15,.03);
       --acctext:#ffffff}
 *{box-sizing:border-box}
 body{background:var(--bg);color:var(--fg);font:14.5px/1.65 -apple-system,"PingFang SC",sans-serif;margin:0;
   -webkit-user-select:none;user-select:none;cursor:default}
 /* 原生窗口默认禁选文本，这里把正文/卡片内容显式设为可选中可复制，
    交互控件（导航/按钮/标签页）保持不可选。 */
 .md,.card,p,li,td,th,pre,blockquote,h1,h2,h3,h4,h5,code,
 .md *,.cite,textarea,input{
   -webkit-user-select:text;user-select:text;cursor:auto}
 nav,.tabs,button,select,label,.tagchip,.grpbar{
   -webkit-user-select:none;user-select:none}
 ::selection{background:var(--accsel)}
 nav{display:flex;gap:4px;padding:10px 22px;background:var(--navbg);
     backdrop-filter:blur(12px);position:sticky;top:0;z-index:9;
     border-bottom:1px solid var(--line);align-items:center;
     overflow-x:auto;overflow-y:hidden}
 /* 窗口变窄时导航栏文字不能被逐字挤破行——"公司档案"/"反馈"/品牌名这些
    多字词一旦换行会拆成"公司档"+"案"这种断字，比整条导航栏横向滚动
    难看得多。全部标成不换行、不收缩，容不下就让 nav 自己横向滚动。 */
 nav a,nav .brand,nav>span,nav>button{white-space:nowrap;flex-shrink:0}
 nav a{color:var(--dim);text-decoration:none;padding:6px 16px;border-radius:9px;
       transition:all .15s}
 nav a:hover{color:var(--fg);background:var(--card2)}
 nav a.on{color:var(--acctext);background:var(--acc);font-weight:600}
 nav .brand{margin-left:auto;color:var(--acc);font-weight:700;letter-spacing:.2px}
 main{max-width:1000px;margin:26px auto;padding:0 22px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);
       padding:18px 22px;margin-bottom:18px}
 .card h3{margin:0 0 12px;font-size:15px}
 .card{overflow-x:auto}
 table.wrap td,table.wrap th{white-space:normal}
 table{width:100%;border-collapse:collapse;font-size:13.5px}
 td,th{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;
       vertical-align:middle;white-space:nowrap}
 tr:last-child td{border-bottom:none}
 tbody tr:hover td{background:rgba(255,255,255,.025)}
 th{color:var(--dim);font-weight:500;font-size:12.5px}
 td.t{max-width:360px}
 .clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
   overflow:hidden;white-space:normal;line-height:1.45;max-height:2.95em;
   overflow-wrap:break-word;word-break:normal}
 /* 之前是 word-break:break-all——中文本来逐字就能换行，不需要这么猛，
    结果连"Palantir"这种英文单词也会被拦腰砍成"Pala"+"nti…"。改成
    overflow-wrap:break-word 之后，正常单词整只换行，只有真的长到一行都
    塞不下的极端情况（没有空格的超长英文/链接）才会退而求其次断词，两头
    都照顾到。 */
 input,select,textarea{background:var(--card2);color:var(--fg);
   border:1px solid var(--bord);border-radius:9px;padding:7px 11px;font:inherit;
   transition:border-color .15s}
 input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}
 select{appearance:none;-webkit-appearance:none;padding-right:28px;
   background-image:url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23909090'/%3E%3C/svg%3E");
   background-repeat:no-repeat;background-position:right 10px center}
 input[type=checkbox],input[type=range]{accent-color:var(--acc)}
 input[type=range]{border:none;background:transparent;padding:0}
 button{background:var(--card2);color:var(--fg);border:1px solid var(--bord);
   border-radius:9px;padding:6px 13px;font:inherit;font-size:13px;cursor:pointer;
   white-space:nowrap;transition:all .15s}
 button:hover{border-color:var(--acc);color:var(--acc)}
 button.primary{background:var(--acc);color:var(--acctext);border:none;font-weight:600;
   padding:7px 16px}
 button.primary:hover{filter:brightness(1.08);color:var(--acctext)}
 .ok{color:var(--ok)} .bad{color:var(--bad)} .dim{color:var(--dim)}
 .st{display:inline-block;padding:1px 11px;border-radius:99px;font-size:12.5px;
     background:var(--card2);white-space:nowrap}
 .st.ok{background:var(--okbg);color:var(--ok)}
 .st.bad{background:var(--badbg);color:var(--bad)}
 .st.run{background:var(--runbg);color:var(--acc);
         animation:pulse 1.6s ease-in-out infinite}
 @keyframes pulse{50%{opacity:.55}}
 .chip{display:inline-block;background:var(--card2);border-radius:99px;
       padding:2px 12px;margin-left:6px;font-size:12.5px;color:var(--dim)}
 .chip b{color:var(--fg)}
 .doodle{color:var(--dim);opacity:.5;float:right;margin:-2px 0 10px 18px}
 .empty{text-align:center;padding:26px;color:var(--dim)}
 .empty .doodle{float:none;margin:0 auto 8px;display:block;opacity:.45}
 .hours{display:grid;grid-template-columns:repeat(12,1fr);gap:6px;max-width:680px}
 .hours label{background:var(--card2);border-radius:9px;text-align:center;
   padding:6px 0;cursor:pointer;border:1px solid var(--bord);transition:all .12s;
   font-size:13px}
 .hours label:hover{border-color:var(--acc)}
 .hours input{display:none}
 .hours input:checked+span{color:var(--acc);font-weight:700}
 .md{max-width:760px;margin:0 auto;font-size:15.5px;line-height:1.75}
 .md img{max-width:100%;border-radius:10px}
 .md h1,.md h2,.md h3{line-height:1.35}
 .md h2{border-bottom:1px solid var(--line);padding-bottom:6px;margin-top:34px}
 .md blockquote{border-left:3px solid var(--acc);margin:10px 0;
   padding:4px 16px;color:var(--dim);background:var(--runbg);
   border-radius:0 8px 8px 0}
 .md pre{background:var(--code);padding:14px;border-radius:10px;overflow:auto}
 .md details{background:var(--card2);border-radius:10px;padding:10px 16px;margin:12px 0}
 .md details summary{cursor:pointer;color:var(--acc)}
 footer{color:var(--dim);opacity:.7;text-align:center;padding:34px;font-size:12.5px}
 .tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
 .tabs button.on{background:var(--acc);color:var(--acctext);border-color:var(--acc);font-weight:600}
 .tagchip{display:inline-block;background:var(--card2);border:1px solid var(--bord);
   border-radius:99px;padding:2px 12px;margin:3px 4px 3px 0;font-size:12.5px;
   color:var(--dim);cursor:pointer;transition:all .12s}
 .tagchip:hover{border-color:var(--acc);color:var(--acc)}
 .tagchip.on{background:var(--acc);color:var(--acctext);border-color:var(--acc)}
 .tagchip.arm{background:#e5484d;color:#fff;border-color:#e5484d}
 .tagchip.co{font-size:11px;padding:1px 9px;opacity:.8}
 .tagchip.co.on{background:#6b7a8f;border-color:#6b7a8f;color:#fff;opacity:1}
 .tagsubtab{background:transparent;border:1px solid transparent;border-radius:8px;
   padding:3px 12px;font-size:12.5px;color:var(--dim);cursor:pointer}
 .tagsubtab:hover{color:var(--fg)}
 .tagsubtab.on{background:var(--card2);border-color:var(--bord);color:var(--fg);font-weight:600}
 .grpbar{position:sticky;top:52px;z-index:8}
 .busy{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99;
   display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px}
 .busy .ring{width:46px;height:46px;border-radius:50%;
   border:4px solid rgba(255,255,255,.25);border-top-color:var(--acc);
   animation:spin 0.9s linear infinite}
 .busy .txt{color:#fff;font-size:15px}
 @keyframes spin{to{transform:rotate(360deg)}}
 .tagwrap{position:relative}
 .taginner{position:absolute;top:0;left:0;right:0;overflow:hidden;
   background:var(--card);border-radius:10px;transition:max-height .18s ease;
   padding:2px 4px 6px;z-index:7}
 .taginner.open{border:1px solid var(--line);box-shadow:0 10px 28px rgba(0,0,0,.25)}
 .tlwrap{overflow-x:auto;padding-bottom:10px}
 .tl{display:flex;gap:22px;padding:8px 6px 14px;position:relative;min-width:max-content}
 .tl::before{content:'';position:absolute;top:31px;left:0;right:0;height:2px;
   background:var(--line);border-radius:2px}
 .tlgroup{min-width:200px;max-width:230px;position:relative;padding-top:46px}
 .tlgroup::before{content:'';position:absolute;top:25px;left:8px;width:14px;height:14px;
   border-radius:50%;background:var(--acc);border:3px solid var(--card)}
 .tldate{position:absolute;top:0;left:0;font-weight:700;font-size:13px;color:var(--acc)}
 .tlcard{background:var(--card2);border:1px solid var(--bord);border-radius:10px;
   padding:9px 12px;margin-bottom:8px;font-size:13px;line-height:1.5}
 .tlcard a{color:var(--fg);text-decoration:none}
 .tlcard a:hover{color:var(--acc)}
 .tlcard .m{color:var(--dim);font-size:12px;margin-top:2px}
 .banner{background:var(--badbg);color:var(--bad);border:1px solid var(--bad);
   border-radius:12px;padding:10px 16px;margin-bottom:16px}
 .banner a{color:var(--bad);font-weight:700}
 img.imgfail{outline:2px dashed var(--bad);min-height:40px;opacity:.4}
 @media (max-width:820px){
   main{padding:0 10px;margin:14px auto}
   .card{padding:12px 14px;border-radius:10px}
   nav{padding:8px 10px} nav a{padding:5px 10px}
   table{font-size:12.5px} td,th{padding:7px 6px}
   td.t{max-width:200px}
   .doodle{display:none}
   .hours{grid-template-columns:repeat(6,1fr)}
 }
 ::-webkit-scrollbar{width:10px;height:10px}
 ::-webkit-scrollbar-thumb{background:var(--scroll);border-radius:6px}
 ::-webkit-scrollbar-track{background:transparent}
</style></head><body>
<nav>
 {% for p,l in [('channels','Channels'),('queue','Queue'),('reports','Reports'),('download','Download'),('api','API'),('settings','Settings')] %}
 <a href="/{{p}}" class="{{'on' if page==p else ''}}">{{l}}</a>{% endfor %}
 {% if dossier_on %}<a href="/companies" class="{{'on' if page=='companies' else ''}}">公司档案</a>{% endif %}
 <a href=https://github.com/bcdddm/youtube-recorder/issues style='color:var(--dim);padding:4px 8px;text-decoration:none' title=GitHub反馈>💬 反馈</a><button id=themebtn style="margin-left:auto;border:none;background:transparent;color:var(--dim);padding:4px 8px;display:flex;align-items:center"></button>
 <span class="brand" style="margin-left:8px">YouTube Recorder</span>
</nav>
<script>
(function(){
  const ICONS = {
    auto:  '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3 a9 9 0 0 1 0 18 z" fill="currentColor" stroke="none"/></svg>',
    light: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M19.4 4.6l-2.1 2.1M6.7 17.3l-2.1 2.1"/></svg>',
    dark:  '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"/></svg>'
  };
  const LABEL = {auto:'跟随系统', light:'日间', dark:'夜间'};
  const KEY = 'ytrec-theme-mode';
  let mode = localStorage.getItem(KEY) || 'auto';
  if (!['auto','light','dark'].includes(mode)) mode = 'auto';
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  const b = document.getElementById('themebtn');
  function apply() {
    const light = mode === 'light' || (mode === 'auto' && mq.matches);
    document.body.dataset.theme = light ? 'light' : 'dark';
    b.innerHTML = ICONS[mode];
    b.title = '主题：' + LABEL[mode] + '（点击切换）';
  }
  mq.addEventListener ? mq.addEventListener('change', () => { if (mode === 'auto') apply(); })
                      : mq.addListener(() => { if (mode === 'auto') apply(); });
  b.onclick = () => {
    mode = mode === 'auto' ? 'light' : mode === 'light' ? 'dark' : 'auto';
    localStorage.setItem(KEY, mode);
    apply();
  };
  apply();
})();
</script>
<main>{{ body|safe }}</main>
<footer>YouTube Recorder v{{ version }} · By Leoluchino</footer>
<script>window.DIGEST_LAST_SEC={{ digest_last_sec }};window.digestBusy=function(label){var o=document.createElement('div');o.className='busy';o.innerHTML='<div class=ring></div><div class=txt id=_dgtxt></div>';document.body.appendChild(o);var t0=Date.now();var last=window.DIGEST_LAST_SEC||0;var el=document.getElementById('_dgtxt');function tick(){var sec=Math.round((Date.now()-t0)/1000);var hint=last>0?('上次用了约 '+last+' 秒'):'首次生成，通常 10–40 秒';el.innerHTML='正在总结 '+(label||'')+'…<br><span style="font-size:12px;opacity:.75">已用 '+sec+' 秒 · '+hint+'</span>';}tick();setInterval(tick,1000);return o;};window.digestSubmit=function(form,label){digestBusy(label);var fd=new FormData(form);fetch(form.getAttribute('action')||'/reports/digest',{method:'POST',body:fd}).then(function(r){return r.text();}).then(function(h){document.open();document.write(h);document.close();}).catch(function(){location.reload();});return false;};</script><!--YTRP--><script>(function(){
  var W = window;
  function cut(s){ var m=s.length; var cs=['?','&','#','/',' ']; for(var i=0;i<cs.length;i++){ var k=s.indexOf(cs[i]); if(k>=0 && k<m) m=k; } return s.substring(0,m); }
  function param(h,name){ var keys=['?'+name+'=','&'+name+'=','#'+name+'=']; for(var i=0;i<keys.length;i++){ var k=h.indexOf(keys[i]); if(k>=0) return cut(h.substring(k+keys[i].length)); } return ''; }
  function allDigits(v){ if(!v) return false; for(var j=0;j<v.length;j++){ if(v[j]<'0'||v[j]>'9') return false; } return true; }
  function startOf(h){
    var v=param(h,'t') || param(h,'start'); if(!v) return 0;
    if(allDigits(v)) return parseInt(v,10);
    var secs=0, num='';
    for(var j=0;j<v.length;j++){ var c=v[j]; if(c>='0'&&c<='9'){ num+=c; } else { var n=parseInt(num||'0',10); if(c==='h')secs+=n*3600; else if(c==='m')secs+=n*60; else if(c==='s')secs+=n; num=''; } }
    if(num) secs+=parseInt(num,10);
    return secs;
  }
  function vidOf(h){
    if(!h) return '';
    var s=h;
    if(s.indexOf('watch?v=')>=0) return cut(s.split('watch?v=')[1]);
    if(s.indexOf('youtu.be/')>=0) return cut(s.split('youtu.be/')[1]);
    var keys=['/embed/','/live/','/shorts/','/v/'];
    for(var i=0;i<keys.length;i++){ if(s.indexOf(keys[i])>=0) return cut(s.split(keys[i])[1]); }
    if(s.indexOf('v=')>=0) return cut(s.split('v=')[1]);
    return '';
  }
  function isExt(h){ if(!h) return false; var s=h.toLowerCase(); return s.indexOf('http://')===0||s.indexOf('https://')===0||s.indexOf('//')===0; }
  function isLocal(h){ var s=h.toLowerCase(); return s.indexOf('127.0.0.1')>=0||s.indexOf('localhost')>=0; }
  var box=null, titleEl=null, big=false, dragging=false, dx=0, dy=0;
  function ensure(){
    if(box) return;
    var st=document.createElement('style');
    st.textContent = ''
      + '.ytrpip{position:fixed;z-index:99999;right:18px;bottom:18px;width:440px;background:#000;border-radius:12px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.15);display:none}'
      + '.ytrpip.big{right:50%;bottom:50%;transform:translate(50%,50%);width:min(1000px,86vw)}'
      + '.ytrpip .bar{display:flex;align-items:center;gap:6px;padding:6px 8px;background:#181818;color:#eee;font:13px system-ui;cursor:move;user-select:none}'
      + '.ytrpip .bar .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.85}'
      + '.ytrpip .bar button{background:#2a2a2a;color:#eee;border:0;border-radius:6px;padding:3px 9px;cursor:pointer;font-size:13px}'
      + '.ytrpip .bar button:hover{background:#3a3a3a}'
      + '.ytrpip .wrap{position:relative;width:100%;aspect-ratio:16/9;background:#000}'
      + '.ytrpip iframe{position:absolute;inset:0;width:100%;height:100%;border:0}';
    document.head.appendChild(st);
    box=document.createElement('div');
    box.className='ytrpip';
    box.innerHTML = '<div class=bar><span class=t>播放</span><button data-a=size title=放大/画中画>⤢</button><button data-a=ext title=在浏览器打开>↗</button><button data-a=close title=返回>✕</button></div><div class=wrap></div>';
    document.body.appendChild(box);
    titleEl = box.querySelector('.t');
    var bar = box.querySelector('.bar');
    box.addEventListener('click', function(e){
      var b=e.target.closest ? e.target.closest('button') : null;
      if(!b) return;
      var a=b.getAttribute('data-a');
      if(a==='close') closep();
      else if(a==='size'){ big=!big; box.classList.toggle('big', big); }
      else if(a==='ext'){ var u=box.getAttribute('data-url')||''; if(u) openExt(u); closep(); }
    });
    bar.addEventListener('mousedown', function(e){
      if(e.target.closest('button')) return;
      dragging=true; big=false; box.classList.remove('big');
      var r=box.getBoundingClientRect();
      box.style.right='auto'; box.style.bottom='auto';
      box.style.left=r.left+'px'; box.style.top=r.top+'px';
      dx=e.clientX-r.left; dy=e.clientY-r.top; e.preventDefault();
    });
    W.addEventListener('mousemove', function(e){
      if(!dragging) return;
      box.style.left=Math.max(0,Math.min(W.innerWidth-60,e.clientX-dx))+'px';
      box.style.top=Math.max(0,Math.min(W.innerHeight-30,e.clientY-dy))+'px';
    });
    W.addEventListener('mouseup', function(){ dragging=false; });
  }
  function closep(){ if(!box) return; box.querySelector('.wrap').innerHTML=''; box.style.display='none'; }
  function openPlayer(id, start, label, url){
    ensure();
    var wrap=box.querySelector('.wrap');
    var f=document.createElement('iframe');
    f.setAttribute('allow','autoplay; encrypted-media; picture-in-picture; fullscreen');
    f.setAttribute('allowfullscreen','');
    var src='https://www.youtube.com/embed/'+id+'?autoplay=1&rel=0';
    if(start>0) src += '&start='+start;
    f.src=src;
    wrap.innerHTML=''; wrap.appendChild(f);
    titleEl.textContent = label || '播放';
    box.setAttribute('data-url', url || ('https://www.youtube.com/watch?v='+id));
    box.style.display='block';
  }
  function openExt(u){ if(W.pywebview && W.pywebview.api && W.pywebview.api.open_external){ W.pywebview.api.open_external(u); } else { W.open(u,'_blank'); } }
  document.addEventListener('click', function(e){
    var a=(e.target && e.target.closest) ? e.target.closest('a') : null;
    if(!a) return;
    var h=a.getAttribute('href');
    if(!isExt(h) || isLocal(h)) return;
    var id=vidOf(h);
    if(id && id.length>=8){ e.preventDefault(); openPlayer(id, startOf(h), (a.textContent||'').trim().slice(0,60), h); }
    else { e.preventDefault(); openExt(h); }
  }, true);
  // 表单提交（删除点位/置顶/折叠等）默认会整页刷新，滚动条跳回顶部——
  // 记住提交前的滚动位置，刷新回来后原样恢复，不要乱动用户正在看的位置
  var SCROLL_KEY = 'ytrec_scroll_' + location.pathname;
  document.addEventListener('submit', function(){
    try { sessionStorage.setItem(SCROLL_KEY, String(W.scrollY)); } catch(e){}
  }, true);
  try {
    var savedY = sessionStorage.getItem(SCROLL_KEY);
    if (savedY !== null) {
      sessionStorage.removeItem(SCROLL_KEY);
      W.scrollTo(0, parseInt(savedY, 10) || 0);
    }
  } catch(e){}
})();</script><!--/YTRP--></body></html>"""


# --- Notion 风手绘线稿插图（stroke 跟随主题色，手绘抖动路径） -----------------

def _svg(inner: str, w: int = 96, vb: str = "0 0 100 84") -> str:
    """Keith Haring 风：粗轮廓、动感小人、放射能量线。"""
    return (f'<svg class=doodle width={w} viewBox="{vb}" fill="none" '
            f'stroke="currentColor" stroke-width="5" stroke-linecap="round" '
            f'stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg">{inner}</svg>')


_E = 'stroke-width="3.2"'  # 能量线略细

DOODLES = {
    # 频道：小人高举电视欢呼
    "channels": _svg(
        '<path d="M32 10 h36 v18 h-36 z"/>'
        '<path d="M44 10 l-5 -7 M56 10 l5 -7"/>'
        '<circle cx="50" cy="40" r="7"/>'
        '<path d="M50 47 v14 M50 50 L36 32 M50 50 L64 32 '
        'M50 61 L38 78 M50 61 L62 78"/>'
        f'<path {_E} d="M20 42 l-8 3 M80 42 l8 3 M24 22 l-8 -5 M76 22 l8 -5 '
        'M14 60 l-7 5 M86 60 l7 5"/>'),
    # 队列：两个小人跳着传箱子
    "queue": _svg(
        '<circle cx="24" cy="20" r="7"/>'
        '<path d="M25 27 L29 48 M27 33 L44 26 M29 48 L18 66 M29 48 L40 64"/>'
        '<path d="M44 18 h16 v14 h-16 z"/>'
        '<circle cx="78" cy="18" r="7"/>'
        '<path d="M77 25 L73 47 M75 31 L60 26 M73 47 L62 65 M73 47 L85 63"/>'
        f'<path {_E} d="M10 30 l-6 -3 M92 28 l6 -3 M14 72 h9 M60 72 h9 '
        'M50 40 v7 M46 44 h8"/>'),
    # 报告：小人举着大字报起舞
    "reports": _svg(
        '<path d="M32 6 h36 v22 h-36 z"/>'
        f'<path {_E} d="M40 13 h20 M40 21 h13"/>'
        '<circle cx="50" cy="42" r="7"/>'
        '<path d="M50 49 v13 M50 52 L34 30 M50 52 L66 30 '
        'M50 62 L38 79 M50 62 L63 77"/>'
        f'<path {_E} d="M24 4 l-7 -3 M76 4 l7 -3 M22 20 l-9 1 M78 20 l9 1"/>'),
    # 设置：小人滚大齿轮
    "settings": _svg(
        '<circle cx="34" cy="52" r="17"/><circle cx="34" cy="52" r="5"/>'
        f'<path {_E} d="M34 35 v-8 M34 69 v8 M17 52 h-8 M51 52 h8 '
        'M22 40 l-6 -6 M46 64 l6 6 M46 40 l6 -6 M22 64 l-6 6"/>'
        '<circle cx="74" cy="26" r="7"/>'
        '<path d="M73 33 L69 52 M71 38 L55 46 M69 52 L60 70 M69 52 L80 68"/>'
        f'<path {_E} d="M90 14 l6 -5 M88 34 l8 2"/>'),
    # 空状态：躺平休息的小人 + Z Z
    "empty": _svg(
        '<circle cx="26" cy="56" r="7"/>'
        '<path d="M33 58 L58 60 M58 60 L76 56 M40 59 L36 48 '
        'M58 60 L66 72 M76 56 L88 64"/>'
        f'<path {_E} d="M56 30 h11 l-11 12 h11 M74 16 h8 l-8 9 h8 '
        'M12 44 l-6 -3 M14 70 h-8"/>', w=116),
}


# 英文翻译层：中文渲染后按词典替换（覆盖全部主要界面文案）
EN_MAP = [
    ("设置 · 按处理流程", "Settings · by pipeline stage"),
    ("一条视频从发现到进库要经过六个环节。下面按发生顺序排列：每段先讲这一步会发生什么，紧接着就是它的可调项。",
     "Each video passes through six stages from discovery to your library. Sections below follow that order: what happens first, then its options."),
    ("⓪ 语言 / Language", "⓪ Language / 语言"),
    ("① 嗅探 · 发现新视频", "① Discover · find new videos"),
    ("② 下载 · 拿到字幕或音频", "② Download · captions or audio"),
    ("③ 转录 · 音频变文字", "③ Transcribe · audio to text"),
    ("④ 整理 · AI 成文与配图", "④ Compose · AI article & screenshots"),
    ("⑤ 阅读与保存 · 写入你的库", "⑤ Read & Save · into your library"),
    ("⑥ AI · 凭证与分工", "⑥ AI · credentials & routing"),
    ("运行模式", "Run mode"), ("发现新视频弹窗", "New-video dialog"),
    ("单轮最多处理", "Max per run"), ("跳过短视频", "Skip shorts"),
    ("转录方式", "Transcriber"), ("MacWhisper 超时", "MacWhisper timeout"),
    ("守候回收", "Collect wait"), ("文章模式", "Article mode"),
    ("原文附加", "Append original"), ("整理附加 Prompt", "Extra prompt"),
    ("自动截图", "Auto screenshots"), ("图片密度", "density"),
    ("1=只截关键画面 … 5=最密：程序保证每个自然段至少一张配图（无命中画面时按该段时间点自动截取）。",
     "1 = key visuals only … 5 = densest: at least one image per section is guaranteed (auto-captured at the section's timestamp when no cue matches)."),
    ("保存模式", "Storage mode"), ("保存根目录", "Root folder"),
    ("Reports 保存位置", "Reports folder"),
    ("保存全部设置", "Save all settings"), ("保存 key", "Save keys"),
    ("已配置", "configured"), ("未配置", "not set"),
    ("处理进度", "Progress"), ("频道", "Channel"), ("标题", "Title"),
    ("时长", "Length"), ("发布", "Published"), ("状态", "Status"),
    ("详情", "Detail"), ("更新于", "Updated"),
    ("立即运行（强制刷新数据）", "Run now (force refresh)"),
    ("粘贴单个视频链接，直接处理…", "Paste a video link to process it…"),
    ("＋添加", "＋Add"), ("重试", "Retry"), ("↩ 取消跳过", "↩ Unskip"), ("跳过", "Skip"),
    ("已发现", "Found"), ("已探测", "Probed"), ("查字幕", "Captions?"),
    ("下载音频", "Downloading"), ("MacWhisper 转录中", "Transcribing"),
    ("文稿就绪", "Transcript ready"), ("文章已生成", "Article ready"),
    ("规划截图", "Planning shots"), ("截图完成", "Shots ready"),
    ("写入中", "Writing"), ("校验中", "Verifying"), ("完成", "Done"),
    ("已跳过", "Skipped"), ("失败", "Failed"), ("放弃", "Given up"),
    ("📖 阅读", "📖 Read"), ("📅 时间轴", "📅 Timeline"), ("🗂 管理", "🗂 Manage"),
    ("搜索标题…", "Search titles…"), ("全部频道", "All channels"),
    ("按日期分组", "Group by date"), ("按频道分组", "Group by channel"),
    ("平铺列表", "Flat list"), ("标签：", "Tags: "),
    ("回收站", "Trash"), ("保留 3 天后自动清除", "auto-purged after 3 days"),
    ("恢复", "Restore"), ("全选", "Select all"),
    ("删除所选（入回收站）", "Delete selected (to trash)"),
    ("这里还什么都没有", "Nothing here yet"),
    ("没有匹配的文章", "No matching articles"),
    ("添加频道", "Add channel"), ("已订阅频道", "Subscribed channels"),
    ("批量启用", "Enable selected"), ("批量停用", "Disable selected"),
    ("批量删除", "Delete selected"), ("起始日期", "Since"),
    ("启用", "On"), ("停用", "Off"), ("删除", "Delete"),
    ("订阅该频道", "Subscribe"),
    ("尚未配置 AI 密钥——文章生成与智能截图不可用。", "No AI key configured — article generation and smart screenshots are unavailable. "),
    ("前往设置添加 →", "Add one in Settings →"),
    ("刷新图片", "Reload images"), ("AI 重新总结", "Re-summarize"),
    ("在 Obsidian 打开", "Open in Obsidian"), ("← 返回列表", "← Back"),
    ("就这篇文章向 AI 提问（细节查询，基于完整原文回答）…", "Ask AI about this article (answers grounded in the full transcript)…"),
    ("提问", "Ask"), ("💬 AI 回答", "💬 AI answer"),
    ("软件更新 / Updates", "Updates / 软件更新"),
    ("当前版本", "Current version"), ("检查更新", "Check for updates"),
    ("按 GitHub Release 版本更新（未发布的提交不会推送）", "Pull latest from GitHub and restart"),
    ("已是最新版本", "Already up to date"),
    ("，正在后台更新并重启——约半分钟后重新打开窗口即为新版",
     " — updating and restarting in background; reopen the window in ~30s"),
    ("发现新版本 ", "New release found: "),
    ("检查更新失败（网络或 git 仓库问题）", "Update check failed (network or git issue)"),
    ("原文保留比例", "Verbatim ratio"),
    ("关闭（自由整理）", "Off (free rewrite)"), ("（推荐）", " (recommended)"),
    ("（纯原文分节）", " (pure original, sectioned)"),
    ("全部组", "All groups"), ("设为组", "Set group"), ("组名", "Group"),
    ("🧾 总结", "🧾 Digest"), ("当日情报汇总", "Daily digest"),
    ("← 返回时间轴", "← Back to timeline"),
    ("没有可汇总的文章。", "No articles to digest."),
    ("组：", "Groups: "), ("（无组）", "(ungrouped)"), ("移出组", "Remove from group"),
    ("引用来源", "References"), ("正在总结", "Summarizing"),
    ("已加载缓存", "From cache"), ("新生成", "Fresh"), ("♻ 重新生成", "♻ Regenerate"),
    ("选择现有组…", "Pick a group…"), ("或新建", "or new"),
    ("加入组", "Add to group"), ("选择要移出的组…", "Group to remove…"),
    ("从组移出", "Remove from"),
    ("OpenAI 模型", "OpenAI model"), ("Anthropic 模型", "Anthropic model"),
    ("整理/召回/问答走 OpenAI 时使用", "Used when a stage routes to OpenAI"),
    ("走 Anthropic 时使用；可直接填任意模型名", "Used when routed to Anthropic; any model id accepted"),
    ("（可重排句序）", " (reorder allowed)"),
    ("🔄 刷新模型列表", "🔄 Refresh model list"),
    ("用你的 key 从各家官方 API 拉取当前可用模型", "Fetch currently available models from each provider API"),
    ("模型列表已刷新（共", "Model list refreshed ("),
    ("拉取模型列表部分失败：", "Model list fetch partly failed: "),
    ("走 OpenAI 时使用", "Used when routed to OpenAI"),
    ("走 Anthropic 时使用", "Used when routed to Anthropic"),
    ("模型列表", "Model list"), ("标点方式", "Punctuation"),
    ("AI 重标点（剥标点逐字校验，内容零改动）", "AI re-punctuation (verified char-identical)"),
    ("机械补标点（句界补逗号/句号）", "Mechanical (boundary commas/periods)"),
    ("的全部内容…", " — everything from that day…"),
    ("要覆盖每一条要点，请稍候（约 10–30 秒）", "Covering every point — hold on (10–30s)"),    ('设置 · YouTube Recorder', 'Settings · YouTube Recorder'),
    ('队列 · YouTube Recorder', 'Queue · YouTube Recorder'),
    ('报告 · YouTube Recorder', 'Reports · YouTube Recorder'),
    ('频道 · YouTube Recorder', 'Channels · YouTube Recorder'),
    ("auto:'跟随系统', light:'日间', dark:'夜间'", "auto:'System', light:'Light', dark:'Dark'"),
    ("'主题：' + LABEL[mode] + '（点击切换）'", "'Theme: ' + LABEL[mode] + ' (click to switch)'"),
    ('当前版本 v', 'Current version v'),
    ('发现新版本', 'New version found'),
    ('正在后台更新并重启——约半分钟后重新打开窗口即为新版', 'Updating and restarting in the background — reopen the window in ~30 s for the new version'),
    ('按排班表整点唤醒（睡眠错过的唤醒后合并补跑），逐个检查订阅频道的 RSS。发现新视频后：自动模式直接进入处理，确认模式先在 Queue 列出等你逐条确认——两种模式都能随时跳过单条。', "Wakes on the hourly schedule (missed runs merge after sleep) and checks each subscribed channel's RSS. When new videos appear: auto mode processes them immediately; confirm mode lists them in Queue for per-item approval — either way you can skip any single item."),
    ('自动：发现后直接处理（Queue 中可随时跳过）', 'Auto: process right after discovery (skip anytime in Queue)'),
    ('确认：发现后先列出，逐条确认/跳过', 'Confirm: list first, approve/skip one by one'),
    ('仅发现新视频时弹', 'Only when new videos are found'),
    ('每次运行都弹', 'Every run'),
    ('从不弹窗', 'Never'),
    ('倒计时', 'Countdown'),
    ('（其余留到下一轮，防首跑洪水）', '(the rest wait for the next run — first-run flood control)'),
    (' 条 ', ' videos '),
    ('先探测：有现成字幕就直接抓取——零成本、几秒完成、跳过转录；无字幕才下载原生 m4a 音频。直播和过短的视频自动跳过。', 'Probe first: existing captions are grabbed directly — zero cost, seconds, no transcription; only caption-less videos download native m4a audio. Live streams and too-short videos are skipped automatically.'),
    ('短于', 'Shorter than'),
    ('秒的视频（Shorts）不处理', 's (Shorts) are not processed'),
    ('主路径把音频投进 MacWhisper 监视文件夹并守候出稿（本地、免费）；或直接走 OpenAI API——超 24MB 自动压缩，仍超限则切段、段间 15 秒重叠、按时间码无缝合并。MacWhisper 超时会自动切 API 兜底，不会卡死。', 'The primary path drops audio into the MacWhisper watch folder and waits for the SRT (local, free); or go straight to the OpenAI API — files over 24 MB are compressed, then chunked with 15-second overlaps and merged seamlessly by timecode. A MacWhisper timeout falls back to the API automatically, so nothing gets stuck.'),
    ('MacWhisper 监视文件夹（本地）', 'MacWhisper watch folder (local)'),
    ('OpenAI Whisper API（云端）', 'OpenAI Whisper API (cloud)'),
    ('whisper.cpp（本地）', 'whisper.cpp (local)'),
    ('分钟无出稿 → 自动转 API 兜底', 'min without output → automatic API fallback'),
    ('投稿后', 'After submitting,'),
    ('分钟内每 20 秒检查一次，出稿立即进入下一环节', 'min of checking every 20 s; it moves on the moment the SRT appears'),
    ('完整文稿分块做忠实笔记（并行调用），再全局组稿成文章——不编造事实、每段可溯源到时间码；随后按内容语义截取视频画面插入对应章节（叙述式视频由 AI 推断画面时刻）。', 'The full transcript is chunked into faithful notes (parallel calls), then composed into one article — no invented facts, every section traceable to timecodes; frames are then captured by content semantics and inserted into matching sections (for narration-style videos the AI infers the moments).'),
    ('整理成文（重组结构）', 'Composed article (restructured)'),
    ('忠实清稿（只去口头禅）', 'Faithful cleanup (fillers removed only)'),
    ('AI 改写在前，完整原文以可折叠块附在文末（Obsidian 中默认收起）', 'AI rewrite first; the full transcript is appended as a collapsible block (folded by default in Obsidian)'),
    ('例：多保留讲者的原话引用；结尾加一段『对我的投资组合的启示』；语气偏口语。', 'E.g.: keep more direct quotes; end with a section on portfolio implications; conversational tone.'),
    ('会追加到 AI 整理的系统提示词末尾；忠实性规则（不编造事实）始终优先。', 'Appended to the composing system prompt; faithfulness rules (no invented facts) always take priority.'),
    ('成品原子写入下方位置并读回校验；之后在 Reports 页阅读、按时间轴浏览、按标签筛选，也可以对任何一篇“问 AI”或让它重新总结。删除的文章进回收站，3 天内可恢复。', 'Output is written atomically to the location below and verified by read-back; then read it in Reports — timeline view, tag filters, per-article “Ask AI”, or re-summarize. Deleted articles go to Trash, restorable for 3 days.'),
    ('Obsidian Vault（20-Raw/30-Wiki 分层治理）', 'Obsidian Vault (20-Raw/30-Wiki layered governance)'),
    ('单独文件夹 · Raw+Wiki 分层', 'Standalone folder · Raw+Wiki split'),
    ('单独文件夹 · 纯平铺（原文折叠随文章，无 Raw 副本）', 'Standalone folder · flat (transcript folded inside the article, no Raw copy)'),
    ('Obsidian 库根目录，或任意目标文件夹', 'Obsidian vault root, or any target folder'),
    ('相对根目录，如 30-Wiki', 'Relative to root, e.g. 30-Wiki'),
    ('修改时把现有文章迁移过去', 'Migrate existing articles when changed'),
    ('本软件默认不含任何 API key——密钥由你添加，直接写入 macOS 钥匙串，不经过配置文件。除 API 外还支持两条本机渠道：Claude Code CLI（走你的 Claude 订阅额度，零 API 费）和 Ollama（本地模型，免费离线）。任一渠道可用即可运转，选了本机渠道失败时自动回落到已配置的 API。下面可以给每个 AI 环节分别指定用哪个渠道：', 'No API key ships with this app — you add your own, stored directly in the macOS Keychain, never in config files. Either provider alone works; with both configured they back each other up. Assign a provider per AI stage below:'),
    ('整理成文用', 'Composing'),
    ('截图召回用', 'Shot recall'),
    ('问 AI 用', 'Ask-AI'),
    ('自动（用已配置的，优先 OpenAI）', 'Auto (use whichever is configured; OpenAI first)'),
    ('>自动</option>', '>Auto</option>'),
    ('保存分工', 'Save routing'),
    ('密钥状态', 'Key status'),
    ('OpenAI key（留空=不变）', 'OpenAI key (blank = unchanged)'),
    ('Anthropic key（留空=不变）', 'Anthropic key (blank = unchanged)'),
    ('刷新模型列表', 'Refresh model list'),
    ('拉取模型列表部分失败', 'Model list refresh partially failed'),
    ('个）', ' total)'),
    ('设置已保存', 'Settings saved'),
    ('分工已保存', 'Routing saved'),
    ('已存入钥匙串', 'Saved to Keychain'),
    ('已迁移', 'Migrated'),
    ('迁移失败', 'Migration failed'),
    ('欢迎使用！请先在下方', 'Welcome! Please add your own AI key (OpenAI or Anthropic) under'),
    ('中添加你自己的 AI 密钥（OpenAI 或 Anthropic 任一），添加后全部功能可用。', 'below — everything unlocks once one is added.'),
    ('添加</button>', 'Add</button>'),
    ('只处理此日期之后发布的视频（留空=从现在起）', 'Only process videos published after this date (blank = from now)'),
    ('日期留空 = 从添加时刻起只收新视频，不回填历史。', 'Blank date = only new videos from the moment added; no backfill.'),
    ('批量删除所选频道？已生成的文章会保留（归入手动添加）。', 'Delete the selected channels? Generated articles are kept (moved to Manually added).'),
    ('删除该频道？已生成的文章会保留', 'Delete this channel? Generated articles are kept'),
    ('已删除频道（历史文章保留）', 'Deleted channels (articles kept)'),
    ('请输入 youtube.com 频道链接', 'Enter a youtube.com channel link'),
    ('你手动添加过这些频道的视频——要不要直接订阅？', "You've manually added videos from these channels — subscribe?"),
    ('手动处理过', 'manually processed'),
    ('条视频', ' video(s)'),
    ('新组', 'new group'),
    ('名称', 'Name'),
    ('暂无', 'None yet'),
    ('请选择现有组或输入新组名', 'Pick an existing group or type a new name'),
    ('移出该组', 'Remove from this group'),
    ('已把该频道移出组「', 'Removed this channel from group “'),
    ('个频道加入组「', ' channel(s) added to group “'),
    ('个频道移出', ' channel(s) removed'),
    ('已添加', 'Added'),
    ('手动添加视为已确认，两种模式都直接处理', 'Manual adds count as approved — processed directly in both modes'),
    ('无法从链接中识别视频 ID，请粘贴完整的 YouTube 视频链接', "Couldn't find a video ID in that link — paste a full YouTube video URL"),
    ('订阅后从现在起自动收新视频）', 'subscribing auto-collects new videos from now on)'),
    ('新发现 · 待确认（先确认才会处理）', 'Newly found · awaiting approval (processed only after you approve)'),
    ('全部处理', 'Approve all'),
    ('▶ 处理', '▶ Process'),
    ('数据未变，跳过重渲染', 'unchanged, skip re-render'),
    ('已等待 ${mins} 分钟（出稿后自动继续）', 'Waiting ${mins} min (auto-continues when the SRT arrives)'),
    ('已触发运行，下方列表会自动更新', 'Run started — the list below updates automatically'),
    ('已有一轮正在运行（含守候转录），', 'A run is already in progress (incl. transcript wait), '),
    ('无需重复触发——下方列表实时更新', 'no need to trigger again — the list updates live'),
    ('删除于', 'Deleted'),
    ('剩余', 'Left'),
    (' 天</td>', ' d</td>'),
    ("'小时'+m+'分' : m+'分钟'", "'h '+m+'m' : m+' min'"),
    ('删除这篇文章？将移入回收站，保留 3 天可恢复。', 'Delete this article? It moves to Trash, restorable for 3 days.'),
    ('移入回收站', 'Move to Trash'),
    ('视频发布', 'Published'),
    ('未知', 'Unknown'),
    ('时间轴从左到右：旧 → 新', 'timeline left to right: old → new'),
    ('总结当天全部内容', 'Summarize everything from this day'),
    ('← 横向滚动浏览 · 自动按视频发布日期排列 →', '← Scroll horizontally · ordered by publish date →'),
    ('先勾选要删除的文章', 'Tick the articles to delete first'),
    ('删除所选 ${ids.length} 篇？将移入回收站，3 天内可恢复。', 'Delete ${ids.length} selected? They move to Trash, restorable for 3 days.'),
    ('共 ${rows.length} 篇', '${rows.length} articles total'),
    ('${rows.length} 篇', '${rows.length} articles'),
    ('· 组:${scope}', '· group: ${scope}'),
    ('· ${groups[k].length} 篇', '· ${groups[k].length} articles'),
    ('<th>生成</th>', '<th>Generated</th>'),
    ('标签栏', 'tag bar'),
    ('组胶囊（置顶排，多选）', 'group pills (pinned row, multi-select)'),
    ('正在总结 ', 'Summarizing '),
    ('重新生成', 'Regenerate'),
    ('当日情报汇总（', 'Daily intel digest ('),
    ('返回时间轴', 'Back to timeline'),
    ('重新加载未显示的图片', 'Reload missing images'),
    ('张图片未能加载，可点', ' image(s) failed to load — click'),
    ('思考中…', 'Thinking…'),
    ('回答', 'Answer'),
    ('依据', 'Evidence'),
    ('用当前设置重新生成这篇文章？（原文文稿不变，AI 重新总结并覆盖本篇）', 'Regenerate this article with the current settings? (Transcript unchanged; the AI re-summarizes and overwrites this article)'),
    ('生成失败', 'Generation failed'),
    ('重新总结', 'Re-summarize'),
    ('返回', 'Back'),
    ('硬约束：正文中至少该比例的字符逐字来自原文（AI 只负责选句和过渡，\n被选句子由程序原样拷贝，不足自动补齐；实测值写入文章 frontmatter。\n70% 以下档位允许 AI 重排句子先后组合，70% 及以上严格保持原文语序）。', 'Hard guarantee: at least this share of body characters is copied verbatim from the transcript (the AI only picks sentences and writes bridges; picked sentences are copied by the program and topped up automatically; the measured ratio goes into the article frontmatter. Tiers below 70% let the AI reorder sentences; 70% and above keep the original order).'),

    ('新组名', 'new group name'),
    ('也可以对任何一篇"问 AI"或让它重新总结。删除的文章进回收站，3 天内可恢复。', 'per-article "Ask AI", or re-summarize. Deleted articles go to Trash, restorable for 3 days.'),
    (' 秒</td>', ' s</td>'),
    ('成品原子写入下方位置并读回校验；之后在 Reports 页阅读、按时间轴浏览、按标签筛选，', 'Output is written atomically to the location below and verified by read-back; then read it in Reports — timeline view, tag filters, '),
    ('🏷 AI 归并同义标签', '🏷 AI-merge similar tags'),
    ('归并中…（AI 分析全部标签）', 'Merging… (AI analyzing all tags)'),
    ('已合并 ${d.merged} 个同义标签', 'Merged ${d.merged} similar tags'),
    ('（共 ${d.total} 个）', ' (of ${d.total})'),
    ('归并失败', 'Merge failed'),
    ('⬇ 导出订阅', '⬇ Export subscriptions'),
    ('⬆ 导入订阅', '⬆ Import subscriptions'),
    ('导出含分组/启停/起始日期；导入按频道合并，不会重复添加', 'Export includes groups, enabled state and start dates; import merges by channel — never duplicates'),
    ('导入完成：新增 ', 'Import done: added '),
    (' 个频道，合并 ', ' channel(s), merged groups for '),
    (' 个频道的组', ' channel(s)'),
    ('导入失败：文件不是有效的订阅导出 JSON', 'Import failed: not a valid subscriptions export JSON'),
    ('Claude Code CLI（本机订阅额度，免 API 费）', 'Claude Code CLI (local subscription, no API cost)'),
    ('Ollama（本地模型，免费离线）', 'Ollama (local models, free & offline)'),
    ('Claude Code 模型', 'Claude Code model'),
    ('Ollama 模型', 'Ollama model'),
    ('走本机 Claude Code CLI 时使用（sonnet/opus/haiku 为官方别名）', 'Used when routed to the local Claude Code CLI (sonnet/opus/haiku are official aliases)'),
    ('走本地 Ollama 时使用；点"刷新模型列表"读取已安装模型', 'Used when routed to local Ollama; click "Refresh model list" to read installed models'),
    ('已检测到', 'detected'),
    ('未安装', 'not installed'),
    ('运行中', 'running'),
    ('未运行', 'not running'),
    ('除 API 外还支持两条本机渠道：Claude Code CLI（走你的 Claude 订阅额度，零 API 费）和 Ollama（本地模型，免费离线）。任一渠道可用即可运转，选了本机渠道失败时自动回落到已配置的 API。下面可以给每个 AI 环节分别指定用哪个渠道：', 'Besides APIs, two local channels are supported: the Claude Code CLI (uses your Claude subscription quota, zero API cost) and Ollama (local models, free & offline). Any one working channel is enough; if a local channel fails it falls back to your configured APIs. Assign a channel per AI stage below:'),
    ('代理未运行（启动 CC Switch）', 'proxy down (start CC Switch)'),
    ('显示引用：开', 'Citations: on'),
    ('显示引用：关', 'Citations: off'),
    ('⧉ 复制', '⧉ Copy'),
    ('✓ 已复制', '✓ Copied'),
    ('复制失败', 'Copy failed'),
    ('基于组的总结和改写个性化', 'Group-based summary & rewrite personalization'),
    ('给每个组一条 prompt：该组频道的<b>单篇改写</b>和含该组文章的<b>当日汇总</b>生成时都会注入，并标注来源组（如【组：投资】）。忠实性规则（不编造事实、不因此漏要点）始终优先；留空 = 该组无个性化。改动后旧日报缓存自动失效、按需重新生成。',
     'One prompt per group: injected into <b>single-article rewrites</b> for that group\'s channels and into <b>daily digests</b> containing its articles, labeled with the group name. Faithfulness rules (no invented facts, no dropped points) always win; blank = no personalization. Changing a prompt invalidates old digest caches automatically.'),
    ('还没有任何组——先在 Channels 页给频道分组。', 'No groups yet — group your channels on the Channels page first.'),
    ('例：偏重政策与宏观影响；结尾给出对该主题的跟踪建议', 'E.g.: emphasize policy and macro impact; end with follow-up suggestions'),
    ('保存组个性化', 'Save group personalization'),
    ('组个性化已保存', 'Group personalization saved'),
    ('订阅 · 导入 / 导出', 'Subscriptions · import / export'),
    ('重新运行', 'Re-run'),
    ('⚠ 已卡在此步 ', '⚠ Stuck here for '),
    (' 分钟，可点重新运行', ' min — click Re-run'),
    ('播放', 'Play'),
    ('放大/画中画', 'Zoom / PiP'),
    ('在浏览器打开', 'Open in browser'),
    ('反馈', 'Feedback'),
    ("已用 '+sec+' 秒", "elapsed '+sec+' s"),
    ("上次用了约 '+last+' 秒", "last time ~'+last+' s"),
    ('首次生成，通常 10–40 秒', 'first run usually takes 10–40 s'),
    ('如 whisper-1 或 FunAudioLLM/SenseVoiceSmall', 'e.g. whisper-1 or FunAudioLLM/SenseVoiceSmall'),
    ('对应钥匙串 ytrec-该名，如 openai / siliconflow', 'matches Keychain item ytrec-<name>, e.g. openai / siliconflow'),
    ('B站 space.bilibili.com/UID / 播客 RSS', 'Bilibili space.bilibili.com/UID / podcast RSS'),
    ('AI 凭证与分工、Qwen/Kimi、语音识别接口已移到独立的', 'AI credentials & routing, Qwen/Kimi and the speech-recognition endpoint now live on the dedicated'),
    ('，让设置更清爽。', ' — keeps Settings lean.'),
    ('API 页', 'API page'),
    ('（订阅额度，免 API 费）', ' (subscription quota, no API cost)'),
    ('（本地，免费离线）', ' (local, free & offline)'),
    ('接口 base_url', 'Endpoint base_url'),
    ('语音识别 · 转录接口', 'Speech recognition · transcription endpoint'),
    ('凭证与分工', 'Credentials & routing'),
    ('用哪个密钥', 'Which key'),
    ('转录模型', 'Transcription model'),
    ('保存密钥', 'Save keys'),
    ('保存转录接口', 'Save transcription endpoint'),
    ('本机渠道：Claude Code CLI', 'Local channels: Claude Code CLI'),
    ('已检测', 'detected'),
    (' key（留空=不变）', ' key (blank = unchanged)'),
    ('Qwen 模型', 'Qwen model'),
    ('Kimi 模型', 'Kimi model'),
    ('模型', 'Model'),
    ('默认走 MacWhisper 或 OpenAI Whisper。也可指向任意 OpenAI 兼容的转录接口（如 SiliconFlow 的 SenseVoice 做中文识别）：填 base_url + 选用哪个密钥 + 模型；base_url 留空即用 OpenAI 官方。注意：只有会返回分段时间码的接口才能得到精确字幕时间轴。',
     'Defaults to MacWhisper or OpenAI Whisper. You can also point to any OpenAI-compatible transcription endpoint (e.g. SiliconFlow SenseVoice for Chinese): set base_url + which key + model; blank base_url = official OpenAI. Note: only endpoints that return segment timecodes give a precise subtitle timeline.'),
    ('密钥写入 macOS 钥匙串，不经过配置文件。支持 OpenAI / Anthropic / Qwen 通义千问 / Kimi 月之暗面 云端 API，以及本机 Claude Code CLI、Ollama。可给每个环节分别指定渠道，选本机渠道失败时自动回落到已配置的 API。',
     'Keys go straight into the macOS Keychain, never config files. Supports OpenAI / Anthropic / Qwen / Kimi cloud APIs plus the local Claude Code CLI and Ollama. Assign a channel per stage; local channels fall back to configured APIs on failure.'),
    ('SiliconFlow（中文语音识别）', 'SiliconFlow (Chinese speech recognition)'),
    ('代理未运行', 'proxy down'),
    ('🏷 AI 想跟你确认几个标签', '🏷 The AI wants to confirm a few tags with you'),
    ('你的选择会被记住，并让之后的归并更精准。', 'Your choices are remembered and make future merges more accurate.'),
    ('应用我的选择', 'Apply my choices'),
    ('应归入哪个标签？', 'should merge into which tag?'),
    ('已应用 ', 'Applied '),
    (' 条人工决定', ' manual decision(s)'),
    ('独立', 'keep separate'),
    ('移除孤儿标签（仅 1 篇文章用到）', 'Remove orphan tags (used by only 1 article)'),
    ('，移除 ${d.orphans_removed} 个孤儿标签', ', removed ${d.orphans_removed} orphan tags'),
    ('标签（点一次选中、再点一次删除）：', 'Tags (click once to select, again to delete):'),
    ('（再点删除）', ' (click again to delete)'),
    ('删除中…', 'Deleting…'),
    ('阅读 · YouTube Recorder', 'Read · YouTube Recorder'),
    ('涉及标签', 'Tags covered'),
    ('（点一次选中、再点一次从当天全部相关文章删除）', ' (click once to select, again to remove from all related articles that day)'),
    ('粘贴链接下载视频（<a href=/download>下载页</a>）用到的保存位置与默认清晰度，在此配置。',
     'Configure the save location and default quality used by paste-a-link download (<a href=/download>Download page</a>) here.'),
    ('粘贴链接下载视频', 'Paste a link to download video'),
    ('与转录/整理管线完全独立——直接把原始视频文件存到本地，不进 Obsidian、不生成文章。支持 yt-dlp 能识别的绝大多数网站（YouTube、B站等）。\n保存位置与默认清晰度在 <a href="/settings#downloads">设置页</a> 修改。当前保存到：',
     'Fully separate from the transcribe/compose pipeline — saves the raw video file locally, no Obsidian write, no article. Works with most sites yt-dlp supports (YouTube, Bilibili, etc.).\nSave location and default quality can be changed on the <a href="/settings#downloads">Settings page</a>. Currently saving to: '),
    ('下载设置', 'Download settings'),
    ('粘贴视频链接…', 'Paste a video link…'),
    ('⬇ 开始下载', '⬇ Start download'),
    ('最高画质（体积最大）', 'Best quality (largest file)'),
    ('4K（2160p）', '4K (2160p)'),
    ('仅音频', 'Audio only'),
    ('保存设置', 'Save location'),
    ('保存到', 'Save to'),
    ('默认清晰度', 'Default quality'),
    ('当前目录：', 'Current folder: '),
    ('在 Finder 打开', 'Open in Finder'),
    ('下载记录', 'Download history'),
    ('请粘贴完整的视频链接（https://…）', 'Paste a full video link (https://…)'),
    ('已开始下载，下方列表会自动更新', 'Download started — the list below updates automatically'),
    ('还没有下载记录', 'No downloads yet'),
    ('排队中', 'Queued'), ('下载中', 'Downloading'), ('合并中', 'Merging'),
    ('完成', 'Done'), ('失败', 'Failed'),
    ('📂 在 Finder 显示', '📂 Show in Finder'),
    ('剩 ', 'left '),
    ('已保存', 'Saved'),
    ('下载 · YouTube Recorder', 'Download · YouTube Recorder'),
    ('加载中…', 'Loading…'),
    ('保存</button>', 'Save</button>'),
    ("公司档案插件", "Company Dossier plugin"),
    ("公司档案", "Company Dossier"),
    ("公司/实体", "Company/Entity"),
    ("记录数", "Entries"),
    ("最近更新", "Last updated"),
    ("还没有公司档案——", "No company dossiers yet — "),
    ("插件会在后台自动建档，写完一轮处理后回来看看。", "the plugin builds them automatically in the background — check back after the next run."),
    ("还没配置保存根目录（Settings → ⑤ 阅读与保存）。", "Vault root isn't configured yet (Settings → ⑤ Read & Save)."),
    ("还没开启，去 ", "is off — turn it on in "),
    (" 里打开。", "."),
    ("← 返回公司列表", "← Back to companies"),
]


_EN_SORTED = None


def _tr(html: str) -> str:
    if cfg_mod.load().get("app.language", "zh") != "en":
        return html
    global _EN_SORTED
    if _EN_SORTED is None:
        _EN_SORTED = sorted(EN_MAP, key=lambda p: len(p[0]), reverse=True)
    for zh, en in _EN_SORTED:
        html = html.replace(zh, en)
    return html


_OLL_CACHE = {"t": 0.0, "ok": False}


def _ollama_alive() -> bool:
    import time, urllib.request
    if time.time() - _OLL_CACHE["t"] < 60:
        return _OLL_CACHE["ok"]
    ok = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version",
                                    timeout=0.8):
            ok = True
    except Exception:
        ok = False
    _OLL_CACHE.update(t=time.time(), ok=ok)
    return ok


def _keys_ok() -> bool:
    if _key_status("openai") or _key_status("anthropic"):
        return True
    from .providers import _claude_cli_path
    return bool(_claude_cli_path()) or _ollama_alive()


def page(title: str, page_id: str, body: str):
    if page_id != "settings" and not _keys_ok():
        body = ('<div class=banner>⚠️ 尚未配置 AI 密钥——文章生成与智能截图不可用。'
                '<a href="/settings">前往设置添加 →</a></div>') + body
    return _tr(render_template_string(BASE, title=title, page=page_id,
                                      body=body, version=__version__,
                                      digest_last_sec=_digest_last_sec(),
                                      dossier_on=cfg_mod.load().get(
                                          "dossier.enabled", False)))


def check_csrf():
    if request.form.get("_csrf") != CSRF:
        # 友好恢复：多为旧页面令牌过期，跳回来源页拿新令牌，而非裸 403
        from flask import make_response
        back = request.referrer or "/reports"
        html = ("<!doctype html><meta charset=utf-8>"
                "<body style='font:15px -apple-system,sans-serif;padding:40px'>"
                "<p>页面已过期，正在刷新… / Session expired, reloading…</p>"
                "<script>location.replace(" + _json_dumps(back) + ")</script>"
                "</body>")
        abort(make_response(html, 403))


def _json_dumps(s):
    import json as _j
    return _j.dumps(s)


def _con():
    return dbm.connect()


# --- Channels -------------------------------------------------------------

@app.route("/")
def index():
    if not _keys_ok():
        return redirect(url_for("settings", firstrun=1))
    return redirect(url_for("channels"))


@app.get("/channels/export")
def channels_export():
    con = _con()
    chans = []
    for r in dbm.list_channels(con):
        if r["channel_id"] == MANUAL_CHANNEL:
            continue
        chans.append({
            "channel_id": r["channel_id"], "url": r["url"], "name": r["name"],
            "enabled": bool(r["enabled"]), "not_before": r["not_before"],
            "groups": _grps_of(r["grp"] if "grp" in r.keys() else ""),
        })
    con.close()
    import json as _j
    from flask import Response
    body = _j.dumps({"app": "YouTube Recorder", "kind": "channel-subscriptions",
                     "version": 1, "exported_at": dbm.now(), "channels": chans},
                    ensure_ascii=False, indent=1)
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition":
            'attachment; filename="youtube-recorder-channels-'
            + dbm.local_date(dbm.now()) + '.json"'})


@app.post("/channels/import")
def channels_import():
    check_csrf()
    import json as _j
    up = request.files.get("file")
    try:
        data = _j.loads(up.read().decode("utf-8"))
        chans = data.get("channels")
        assert isinstance(chans, list)
    except Exception:
        return redirect("/channels?imp=bad")
    con = _con()
    added = merged = 0
    for c in chans:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("channel_id") or "").strip()
        if not cid.startswith("UC") or len(cid) > 40:
            continue
        url = str(c.get("url") or "https://www.youtube.com/channel/" + cid)[:300]
        name = str(c.get("name") or "")[:120] or None
        grps = [g.strip() for g in (c.get("groups") or [])
                if isinstance(g, str) and g.strip()][:20]
        row = con.execute("SELECT grp, name FROM channels WHERE channel_id=?",
                          (cid,)).fetchone()
        if row is None:
            nb = c.get("not_before")
            dbm.add_channel(con, cid, url, name,
                            not_before=nb if isinstance(nb, str) else None)
            if grps:
                con.execute("UPDATE channels SET grp=? WHERE channel_id=?",
                            (_grps_join(grps), cid))
            if c.get("enabled") is False:
                con.execute("UPDATE channels SET enabled=0 WHERE channel_id=?", (cid,))
            added += 1
        else:
            old = _grps_join(_grps_of(row["grp"]))
            new = _grps_join(_grps_of(row["grp"]) + grps)
            if new != old:
                con.execute("UPDATE channels SET grp=? WHERE channel_id=?", (new, cid))
                merged += 1
            if name and not row["name"]:
                con.execute("UPDATE channels SET name=? WHERE channel_id=?", (name, cid))
    con.commit()
    con.close()
    return redirect("/channels?imp=ok&added=" + str(added) + "&merged=" + str(merged))


def _downloads_dest() -> Path:
    cfg = cfg_mod.load()
    d = cfg.get("downloads.dest_dir") or str(Path.home() / "Downloads" / "YouTube Recorder")
    return Path(d).expanduser()


@app.route("/download", methods=["GET", "POST"])
def download_page():
    from . import quickdl
    cfg = cfg_mod.load()
    msg = ""
    if request.method == "POST":
        check_csrf()
        f = request.form
        url = f.get("url", "").strip()
        quality = f.get("quality", cfg.get("downloads.default_quality", "1080p"))
        if not quickdl.valid_url(url):
            msg = '<span class=bad>请粘贴完整的视频链接（https://…）</span>'
        else:
            quickdl.start_download(url, quality, _downloads_dest())
            msg = '<span class="st run">已开始下载，下方列表会自动更新</span>'
    dest = str(_downloads_dest())
    cur_q = cfg.get("downloads.default_quality", "1080p")
    qopts = "".join(
        f'<option value={q} {"selected" if q == cur_q else ""}>{label}</option>'
        for q, label in [("best", "最高画质（体积最大）"), ("2160p", "4K（2160p）"),
                         ("1080p", "1080p"), ("720p", "720p"), ("480p", "480p"),
                         ("audio", "仅音频")])
    body = f"""<div class=card><svg class=doodle width=96 viewBox="0 0 100 84" fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M50 12 v40 M36 38 L50 52 L64 38"/><path d="M20 62 v10 a4 4 0 0 0 4 4 h52 a4 4 0 0 0 4-4 v-10"/></svg>
<h3>粘贴链接下载视频</h3>
<p class=dim>与转录/整理管线完全独立——直接把原始视频文件存到本地，不进 Obsidian、不生成文章。支持 yt-dlp 能识别的绝大多数网站（YouTube、B站等）。
保存位置与默认清晰度在 <a href="/settings#downloads">设置页</a> 修改。当前保存到：<code>{escape(dest)}</code></p>
{f"<p>{msg}</p>" if msg else ""}
<form method=post style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input type=hidden name=_csrf value={CSRF}>
<input name=url placeholder="粘贴视频链接…" style="flex:1;min-width:280px" required>
<select name=quality>{qopts}</select>
<button class=primary>⬇ 开始下载</button>
</form></div>

<div class=card><h3>下载记录</h3><div id=dljobs class=dim>加载中…</div></div>
<script>
function _dlfmt(j) {{
  const st = {{queued:'排队中', downloading:'下载中', merging:'合并中', done:'完成', error:'失败'}}[j.status] || j.status;
  const bar = (j.status === 'downloading' || j.status === 'merging') ?
    `<div style="background:var(--card2);border-radius:6px;height:6px;margin:4px 0;overflow:hidden">
       <div style="background:var(--acc);height:100%;width:${{j.pct}}%"></div></div>` : '';
  const meta = j.status === 'downloading' ? `${{j.pct}}% · ${{j.speed}} · 剩 ${{j.eta}}` :
               j.status === 'error' ? `<span class=bad>${{esc(j.error)}}</span>` :
               j.status === 'done' ? `<button style="font-size:12px;padding:1px 8px" onclick="revealDl('${{esc(j.path)}}')">📂 在 Finder 显示</button>` : '';
  return `<div style="padding:8px 0;border-bottom:1px solid var(--line)">
    <div style="display:flex;justify-content:space-between;gap:8px">
      <span class=t>${{esc(j.title || j.url)}}</span><span class="st ${{j.status==='done'?'ok':j.status==='error'?'bad':'run'}}">${{st}}</span>
    </div>${{bar}}<div class=dim style="font-size:12px">${{meta}}</div></div>`;
}}
function revealDl(path) {{
  fetch('/download/reveal', {{method:'POST',
    headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
    body:'_csrf={CSRF}&path=' + encodeURIComponent(path)}});
}}
let _dlTimer = null;
async function pollDl() {{
  try {{
    const jobs = await (await fetch('/download/jobs.json')).json();
    const box = document.getElementById('dljobs');
    box.innerHTML = jobs.length ? jobs.map(_dlfmt).join('') : '<p class=dim>还没有下载记录</p>';
    const active = jobs.some(j => j.status === 'downloading' || j.status === 'merging' || j.status === 'queued');
    clearTimeout(_dlTimer);
    _dlTimer = setTimeout(pollDl, active ? 1200 : 4000);
  }} catch (e) {{ _dlTimer = setTimeout(pollDl, 4000); }}
}}
pollDl();
</script>"""
    return page("下载", "download", body)


@app.get("/download/jobs.json")
def download_jobs_json():
    from . import quickdl
    from flask import jsonify
    return jsonify(quickdl.list_jobs()[:20])


@app.post("/download/reveal")
def download_reveal():
    check_csrf()
    path = request.form.get("path", "")
    dest = _downloads_dest()
    try:
        p = Path(path).resolve()
        p.relative_to(dest.resolve())
    except (ValueError, OSError):
        return "", 400
    if p.exists():
        import subprocess
        subprocess.run(["open", "-R", str(p)])
    return "", 204


@app.route("/channels", methods=["GET", "POST"])
def channels():
    con = _con()
    msg = ""
    if request.args.get("imp") == "ok":
        msg = ('<span class=ok>导入完成：新增 ' + str(int(request.args.get("added", 0)))
               + ' 个频道，合并 ' + str(int(request.args.get("merged", 0)))
               + ' 个频道的组</span>')
    elif request.args.get("imp") == "bad":
        msg = '<span class=bad>导入失败：文件不是有效的订阅导出 JSON</span>'
    if request.method == "POST":
        check_csrf()
        f = request.form
        ids = f.getlist("cid")

        def _delete(cid):
            # 频道删除：其视频归入"手动添加"以保留已生成的文章记录
            con.execute(
                "INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) "
                "VALUES('MANUAL','','手动添加',0,?)", (dbm.now(),))
            con.execute("UPDATE videos SET channel_id='MANUAL' WHERE channel_id=?",
                        (cid,))
            con.execute("DELETE FROM channels WHERE channel_id=? AND channel_id!='MANUAL'",
                        (cid,))
            con.commit()

        if f.get("act_toggle"):
            con.execute("UPDATE channels SET enabled=1-enabled WHERE channel_id=?",
                        (f["act_toggle"],))
            con.commit()
        elif f.get("act_del"):
            _delete(f["act_del"])
            msg = '<span class=ok>已删除频道（历史文章保留）</span>'
        elif f.get("bulk") == "enable" and ids:
            con.executemany("UPDATE channels SET enabled=1 WHERE channel_id=?",
                            [(i,) for i in ids]); con.commit()
            msg = f'<span class=ok>已启用 {len(ids)} 个频道</span>'
        elif f.get("bulk") == "disable" and ids:
            con.executemany("UPDATE channels SET enabled=0 WHERE channel_id=?",
                            [(i,) for i in ids]); con.commit()
            msg = f'<span class=ok>已停用 {len(ids)} 个频道</span>'
        elif f.get("chip_del"):
            cid_g = f["chip_del"].split("|", 1)
            if len(cid_g) == 2:
                cid2, g2 = cid_g
                row = con.execute("SELECT grp FROM channels WHERE channel_id=?",
                                  (cid2,)).fetchone()
                if row:
                    gs = [x for x in _grps_of(row["grp"]) if x != g2]
                    con.execute("UPDATE channels SET grp=? WHERE channel_id=?",
                                (_grps_join(gs), cid2)); con.commit()
                    msg = '<span class=ok>已把该频道移出组「' + str(escape(g2)) + '」</span>'
        elif f.get("bulk") == "addgroup" and ids:
            gname = (f.get("grpnew", "").strip()
                     or f.get("grpsel", "").strip())[:20]
            if gname:
                qmarks = ",".join("?" * len(ids))
                grp_of = {r["channel_id"]: r["grp"] for r in con.execute(
                    f"SELECT channel_id, grp FROM channels WHERE channel_id IN ({qmarks})",
                    ids)}
                for i in ids:
                    gs = _grps_of(grp_of.get(i, "")) + [gname]
                    con.execute("UPDATE channels SET grp=? WHERE channel_id=?",
                                (_grps_join(gs), i))
                con.commit()
                msg = ('<span class=ok>已把 ' + str(len(ids))
                       + ' 个频道加入组「' + str(escape(gname)) + '」</span>')
            else:
                msg = '<span class=bad>请选择现有组或输入新组名</span>'
        elif f.get("bulk") == "removegroup" and ids:
            target = f.get("rmgrp", "").strip()
            qmarks = ",".join("?" * len(ids))
            grp_of = {r["channel_id"]: r["grp"] for r in con.execute(
                f"SELECT channel_id, grp FROM channels WHERE channel_id IN ({qmarks})",
                ids)}
            for i in ids:
                if i not in grp_of:
                    continue
                gs = ([] if target == "__ALL__" else
                      [x for x in _grps_of(grp_of[i]) if x != target])
                con.execute("UPDATE channels SET grp=? WHERE channel_id=?",
                            (_grps_join(gs), i))
            con.commit()
            label = ("全部组" if target == "__ALL__"
                     else '组「' + str(escape(target)) + '」')
            msg = ('<span class=ok>已把 ' + str(len(ids))
                   + ' 个频道移出' + label + '</span>')
        elif f.get("bulk") == "delete" and ids:
            for i in ids:
                _delete(i)
            msg = f'<span class=ok>已删除 {len(ids)} 个频道（历史文章保留）</span>'
        elif f.get("suggest"):
            cid = f["suggest"]
            row = con.execute(
                "SELECT src_channel_name, COUNT(*) n FROM videos "
                "WHERE src_channel_id=? GROUP BY src_channel_id", (cid,)).fetchone()
            if row:
                dbm.add_channel(con, cid,
                                "https://www.youtube.com/channel/" + cid,
                                row["src_channel_name"])
                msg = ('<span class=ok>已订阅 '
                       + str(escape(row["src_channel_name"] or cid)) + '</span>')
        elif f.get("url"):
            url = f.get("url", "").strip()
            from . import platforms as _pf
            _det = _pf.detect(url)
            if _det and _det.get("platform") == "bilibili":
                _cid = _det["channel_id"]
                _name = _pf.bili_name(_cid) or ("B站 " + _cid.split(":")[-1])
                _nb = f.get("not_before") or None
                if _nb and len(_nb) == 10:
                    _nb += "T00:00:00Z"
                dbm.add_channel(con, _cid, _det["url"], _name, not_before=_nb)
                con.execute("UPDATE channels SET platform='bilibili' WHERE channel_id=?", (_cid,))
                con.commit()
                msg = f'<span class=ok>已添加 B站 {escape(_name)}</span>'
            elif url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/"):
                try:
                    from .cli import _resolve_channel_id
                    cid, name = _resolve_channel_id(url)
                    nb = f.get("not_before") or None
                    if nb and len(nb) == 10:
                        nb += "T00:00:00Z"
                    dbm.add_channel(con, cid, url, name, not_before=nb)
                    msg = f'<span class=ok>已添加 {escape(name)}</span>'
                except SystemExit as e:
                    msg = f'<span class=bad>{escape(str(e))}</span>'
            else:
                _pi = _pf.podcast_info(url)
                if _pi:
                    import hashlib as _h
                    _cid = "pod:" + _h.sha1(url.encode("utf-8")).hexdigest()[:14]
                    _nb = f.get("not_before") or None
                    if _nb and len(_nb) == 10:
                        _nb += "T00:00:00Z"
                    dbm.add_channel(con, _cid, url, _pi[0], not_before=_nb)
                    con.execute("UPDATE channels SET platform='podcast' WHERE channel_id=?", (_cid,))
                    con.commit()
                    msg = f'<span class=ok>已添加 播客 {escape(_pi[0])}（{_pi[1]} 集）</span>'
                else:
                    msg = '<span class=bad>无法识别：请粘贴 YouTube 频道 / B站 space.bilibili.com/UID / 播客 RSS 链接</span>'


    parts = []
    for r in dbm.list_channels(con):
        if r["channel_id"] == "MANUAL":
            continue  # 伪频道不进订阅列表，其视频来源进下方 Suggestion
        cid = escape(r["channel_id"])
        is_manual = False
        chk = ("<input type=checkbox name=cid value='" + str(cid) + "'"
               + (" disabled" if is_manual else "") + ">")
        stbadge = ("<span class='st ok'>启用</span>" if r["enabled"]
                   else "<span class=st>停用</span>")
        if is_manual:
            acts = ""
        else:
            confirm_js = ("return confirm('删除该频道？已生成的文章会保留"
                          "（归入手动添加）。')")
            acts = ("<button name=act_toggle value='" + str(cid) + "'>"
                    + ("停用" if r["enabled"] else "启用") + "</button> "
                    + '<button name=act_del value=\'' + str(cid) + '\' '
                    + 'onclick="' + confirm_js + '">删除</button>')
        try:
            grps = _grps_of(r["grp"])
        except (KeyError, IndexError):
            grps = []
        chips = "".join(
            "<span class=chip style='margin-left:0;margin-right:4px'>"
            + str(escape(g))
            + " <button name=chip_del value='" + str(cid) + "|" + str(escape(g))
            + "' style='border:none;background:none;padding:0 2px;font-size:11px'"
            + " title='移出该组'>×</button></span>"
            for g in grps)
        parts.append(
            "<tr><td>" + chk + "</td>"
            + "<td>" + str(escape(r["name"] or ""))
            + (("<br>" + chips) if chips else "") + "</td>"
            + "<td class=dim>" + str(cid) + "</td>"
            + "<td class=dim>" + str(escape(dbm.local_date(r["not_before"]))) + "</td>"
            + "<td>" + stbadge + "</td><td>" + acts + "</td></tr>")
    rows = "".join(parts)
    all_grps = sorted({g for row in dbm.list_channels(con)
                       for g in _grps_of(row["grp"] if "grp" in row.keys() else "")})
    grp_options = ('<option value="">选择现有组…</option>'
                   + "".join('<option>' + str(escape(g)) + '</option>' for g in all_grps))
    rm_options = ('<option value="">选择要移出的组…</option>'
                  + "".join('<option>' + str(escape(g)) + '</option>' for g in all_grps)
                  + '<option value="__ALL__">全部组</option>')
    suggs = con.execute(
        "SELECT src_channel_id, src_channel_name, COUNT(*) n FROM videos "
        "WHERE channel_id='MANUAL' AND src_channel_id IS NOT NULL "
        "AND src_channel_id NOT IN (SELECT channel_id FROM channels) "
        "GROUP BY src_channel_id ORDER BY n DESC").fetchall()
    if suggs:
        srows = "".join(
            "<tr><td>" + str(escape(g["src_channel_name"] or g["src_channel_id"]))
            + "</td><td class=dim>" + str(escape(g["src_channel_id"])) + "</td>"
            + "<td class=dim>手动处理过 " + str(g["n"]) + " 条视频</td>"
            + "<td><button class=primary name=suggest value='"
            + str(escape(g["src_channel_id"])) + "'>＋ 订阅该频道</button></td></tr>"
            for g in suggs)
        sugg_html = ("<div class=card><h3>💡 Suggestion</h3>"
                     "<p class=dim>你手动添加过这些频道的视频——要不要直接订阅？"
                     "（订阅后从现在起自动收新视频）</p>"
                     "<form method=post><input type=hidden name=_csrf value=" + CSRF + ">"
                     "<table><tr><th>频道</th><th>ID</th><th>依据</th><th></th></tr>"
                     + srows + "</table></form></div>")
    else:
        sugg_html = ""
    body = f"""
<div class=card>{DOODLES['channels']}<h3>添加频道</h3>{msg}
<form method=post style="display:flex;gap:8px;flex-wrap:wrap">
<input type=hidden name=_csrf value={CSRF}>
<input name=url placeholder="YouTube 频道 / B站 space.bilibili.com/UID / 播客 RSS" style="flex:1;min-width:280px">
<input name=not_before type=date title="只处理此日期之后发布的视频（留空=从现在起）">
<button class=primary>添加</button></form>
<p class=dim>日期留空 = 从添加时刻起只收新视频，不回填历史。</p></div>
<div class=card><h3>已订阅频道</h3>

<form method=post>
<input type=hidden name=_csrf value={CSRF}>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
<label><input type=checkbox onchange="document.querySelectorAll('input[name=cid]:not(:disabled)').forEach(c=>c.checked=this.checked)"> 全选</label>
<button name=bulk value=enable>批量启用</button>
<button name=bulk value=disable>批量停用</button>
<button name=bulk value=delete
 onclick="return confirm('批量删除所选频道？已生成的文章会保留（归入手动添加）。')">批量删除</button>
</div>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;
 padding-top:8px;border-top:1px solid var(--line)">
<span class=dim>组：</span>
<select name=grpsel>{grp_options}</select>
<span class=dim>或新建</span> <input name=grpnew size=8 placeholder="新组名">
<button name=bulk value=addgroup>加入组</button>
<span style="flex-basis:8px"></span>
<select name=rmgrp>{rm_options}</select>
<button name=bulk value=removegroup>从组移出</button>
</div>
<table><tr><th></th><th>名称</th><th>ID</th><th>起始日期</th><th>状态</th><th></th></tr>
{rows or '<tr><td colspan=6 class=dim>暂无</td></tr>'}</table>
</form></div>
{sugg_html}"""
    con.close()
    return page("频道", "channels", body)


# --- Queue ------------------------------------------------------------------

def _grps_of(raw: str) -> list:
    return sorted({g.strip() for g in (raw or "").split(",") if g.strip()})


def _grps_join(gs) -> str:
    return ",".join(sorted({g.strip() for g in gs if g and g.strip()}))


MANUAL_CHANNEL = "MANUAL"
_VID_RE = __import__("re").compile(
    r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


@app.route("/add-video", methods=["POST"])
def add_video():
    check_csrf()
    m = _VID_RE.search(request.form.get("url", "").strip())
    if not m:
        return redirect(url_for("queue", badurl=1))
    vid = m.group(1)
    con = _con()
    con.execute(
        "INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) "
        "VALUES(?,?,?,0,?)", (MANUAL_CHANNEL, "", "手动添加", dbm.now()))
    con.commit()
    dbm.upsert_discovered(con, vid, MANUAL_CHANNEL, vid, None)
    dbm.approve_video(con, vid)   # 手动添加视为已确认，两种模式都直接处理
    con.close()
    return redirect(url_for("run_now_get"))


def _run_busy() -> bool:
    """非阻塞探测：是否已有一轮 run 持有进程锁。"""
    from .lock import AlreadyRunning, ProcessLock
    try:
        with ProcessLock():
            return False
    except AlreadyRunning:
        return True


def _ver_tuple(v: str) -> tuple:
    import re as _re
    nums = _re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def _models_cache_path():
    from .paths import APP_SUPPORT
    return APP_SUPPORT / "models.json"


def _load_models() -> dict:
    import json as _j
    p = _models_cache_path()
    if p.exists():
        try:
            return _j.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"openai": [], "anthropic": []}


@app.route("/ai/models", methods=["POST"])
def refresh_models():
    """用用户的 key 调各家官方 models API，拉取真实模型列表并缓存。"""
    check_csrf()
    from .creds import get_key
    import json as _j
    out = _load_models()
    errs = []
    if get_key("openai"):
        try:
            from openai import OpenAI
            ms = [m.id for m in OpenAI(api_key=get_key("openai")).models.list()]
            out["openai"] = sorted(
                m for m in ms
                if (m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")))
                and not any(x in m for x in ("audio", "realtime", "tts",
                                             "transcribe", "image", "search")))
        except Exception as e:
            errs.append(f"openai: {str(e)[:80]}")
    if get_key("anthropic"):
        try:
            import anthropic
            ms = anthropic.Anthropic(api_key=get_key("anthropic")).models.list()
            out["anthropic"] = sorted(m.id for m in ms.data)
        except Exception as e:
            errs.append(f"anthropic: {str(e)[:80]}")
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                    timeout=2) as resp:
            tags = _j.loads(resp.read().decode("utf-8"))
        out["ollama"] = sorted(m["name"] for m in tags.get("models", []))
    except Exception:
        pass  # ollama 未运行则跳过，不算错误
    _models_cache_path().write_text(_j.dumps(out, ensure_ascii=False),
                                    encoding="utf-8")
    n = len(out.get("openai", [])) + len(out.get("anthropic", []))
    return redirect(url_for("api_page",
                            models=("err:" + ";".join(errs)) if errs else str(n)))


@app.post("/set-language")
def set_language():
    check_csrf()
    lang = request.form.get("lang", "zh")
    if lang in ("zh", "en"):
        c = cfg_mod.load()
        c.data.setdefault("app", {})["language"] = lang
        cfg_mod.save(c)
    return "ok"


@app.route("/update", methods=["POST"])
def app_update():
    """按已发布的 Release 标签更新：只有打了 vX.Y.Z tag 并发布的版本
    才会推送给用户；main 上未发布的提交不触发更新。"""
    check_csrf()
    proj = Path(__file__).resolve().parents[2]
    try:
        r = subprocess.run(["git", "-C", str(proj), "ls-remote", "--tags",
                            "origin"], capture_output=True, text=True, timeout=30)
        tags = [line.rsplit("refs/tags/", 1)[-1].replace("^{}", "")
                for line in r.stdout.splitlines() if "refs/tags/v" in line]
        if not tags:
            return redirect(url_for("settings", upd="latest"))
        latest = max(set(tags), key=_ver_tuple)
    except Exception:
        return redirect(url_for("settings", upd="err"))
    if _ver_tuple(latest) <= _ver_tuple(__version__):
        return redirect(url_for("settings", upd="latest"))
    script = proj / "app" / "scripts" / "self_update.sh"
    subprocess.Popen(["/bin/bash", str(script), latest], start_new_session=True)
    return redirect(url_for("settings", upd=f"pulling{latest}"))


@app.route("/run-now", methods=["POST"])
def run_now():
    check_csrf()
    if _run_busy():
        return redirect(url_for("queue", busy=1))
    from .paths import APP_SUPPORT, cli_launch_argv
    argv, cwd = cli_launch_argv("run", "--once", "--headless")
    subprocess.Popen(
        argv, cwd=cwd,
        stdout=open(APP_SUPPORT / "logs" / "manual-run.log", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True)
    return redirect(url_for("queue", started=1))


STAGE_ZH = {
    "discovered": "① 已发现", "metadata_ready": "② 已探测",
    "caption_check": "② 查字幕", "audio_queued": "③ 下载音频",
    "awaiting_transcription": "③ MacWhisper 转录中",
    "transcript_ready": "④ 文稿就绪", "article_ready": "⑤ 文章已生成",
    "visual_planned": "⑥ 规划截图", "frames_ready": "⑥ 截图完成",
    "package_ready": "⑦ 写入中", "written": "⑦ 校验中", "verified": "✓ 完成",
    "ignored": "已跳过", "failed": "✗ 失败", "dead_letter": "✗ 放弃",
}
# 步骤全流程：①发现 ②探测/字幕 ③下载+转录 ④校验文稿 ⑤AI成文 ⑥截图 ⑦写入Obsidian


def _fmt_dur(sec):
    if not sec:
        return ""
    return f"{sec//60}:{sec%60:02d}" if sec < 3600 else f"{sec//3600}:{sec%3600//60:02d}:{sec%60:02d}"


def _queue_data(con) -> dict:
    counts = dbm.counts_by_status(con)
    pending = con.execute(
        "SELECT v.*, c.name cname FROM videos v LEFT JOIN channels c USING(channel_id) "
        "WHERE v.status='discovered' AND v.approved=0 "
        "ORDER BY v.published_at DESC").fetchall()
    vids = con.execute(
        "SELECT v.*, c.name cname,"
        " (SELECT detail FROM attempts a WHERE a.video_id=v.video_id"
        "  ORDER BY a.id DESC LIMIT 1) last_detail,"
        " (SELECT stage FROM attempts a WHERE a.video_id=v.video_id"
        "  ORDER BY a.id DESC LIMIT 1) last_stage "
        "FROM videos v LEFT JOIN channels c USING(channel_id) "
        "WHERE NOT (v.status='discovered' AND v.approved=0) "
        "ORDER BY v.updated_at DESC LIMIT 80").fetchall()
    def _row(r):
        d = dict(r)
        d["pub_local"] = dbm.local_date(d.get("published_at"))
        d["upd_local"] = dbm.local_time(d.get("updated_at"))
        return d
    return {"counts": counts,
            "pending": [_row(r) for r in pending],
            "rows": [_row(r) for r in vids]}


@app.route("/queue.json")
def queue_json():
    con = _con()
    d = _queue_data(con)
    con.close()
    from flask import jsonify
    return jsonify(d)


@app.route("/queue", methods=["GET", "POST"])
def queue():
    con = _con()
    if request.method == "POST":
        check_csrf()
        f = request.form
        if f.get("approve"):
            dbm.approve_video(con, f["approve"])
        elif f.get("approve_all"):
            con.execute(
                "UPDATE videos SET approved=1, updated_at=? "
                "WHERE status='discovered' AND approved=0", (dbm.now(),))
            con.commit()
        elif f.get("skip"):
            v = dbm.get_video(con, f["skip"])
            if v:
                try:
                    dbm.set_status(con, f["skip"], st.IGNORED,
                                   error_code="user_skip")
                except st.TransitionError:
                    pass  # already terminal / being written — too late to skip
        elif f.get("retry"):
            v = dbm.get_video(con, f["retry"])
            if v and v["status"] in (st.FAILED, st.DEAD_LETTER, st.IGNORED):
                dbm.set_status(con, f["retry"], f.get("stage", st.DISCOVERED))
            elif v and v["status"] not in st.TERMINAL_STAGES:
                try:
                    dbm.set_status(con, f["retry"], st.FAILED, error_code="user_rerun")
                    dbm.set_status(con, f["retry"], st.DISCOVERED)
                except st.TransitionError:
                    pass
        if f.get("approve") or f.get("approve_all") or f.get("retry"):
            con.close()
            return redirect(url_for("run_now_get"))
    started = ('<span class=ok id=started>已触发运行，下方列表会自动更新</span>'
               if request.args.get("started") else "")
    if request.args.get("badurl"):
        started = '<span class=bad>无法从链接中识别视频 ID，请粘贴完整的 YouTube 视频链接</span>'
    if request.args.get("busy"):
        started = ('<span class="st run">已有一轮正在运行（含守候转录），'
                   '无需重复触发——下方列表实时更新</span>')
    body = f"""<div class=card style="display:flex;align-items:center;gap:16px">
<form method=post action=/run-now style="margin:0">
<input type=hidden name=_csrf value={CSRF}>
<button class=primary>⟳ 立即运行（强制刷新数据）</button></form>
<form method=post action=/add-video style="margin:0;display:flex;gap:8px;flex:1;min-width:260px">
<input type=hidden name=_csrf value={CSRF}>
<input name=url placeholder="粘贴单个视频链接，直接处理…" style="flex:1">
<button>＋添加</button></form>
{started}<span style="flex:1"></span><span id=chips class=dim></span></div>
<div class=card id=pendingcard style="display:none">
<h3>🆕 新发现 · 待确认（先确认才会处理）</h3>
<form method=post style="margin:6px 0"><input type=hidden name=_csrf value={CSRF}>
<button class=primary name=approve_all value=1>全部处理</button></form>
<table><thead><tr><th>频道</th><th>标题</th><th>发布</th><th></th></tr></thead>
<tbody id=pending></tbody></table></div>
<div class=card>{DOODLES['queue']}<h3>处理进度</h3>
<table><thead><tr><th>频道</th><th>标题</th><th>时长</th><th>发布</th>
<th>状态</th><th>详情</th><th>更新于</th><th></th></tr></thead>
<tbody id=rows></tbody></table></div>
<script>
const CSRF_T = "{CSRF}";
const Z = {__import__('json').dumps(STAGE_ZH, ensure_ascii=False)};
function esc(s) {{ return (s||'').replace(/[&<>"]/g, c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]); }}
function btn(name,val,label,cls) {{ return `<form method=post style="display:inline">
<input type=hidden name=_csrf value=${{CSRF_T}}><input type=hidden name=${{name}} value="${{esc(val)}}">
<button class="${{cls||''}}">${{label}}</button></form>`; }}
let _lastQ = '';
async function refresh() {{
  try {{
    const _txt = await (await fetch('/queue.json')).text();
    if (_txt === _lastQ) return;   // 数据未变，跳过重渲染
    _lastQ = _txt;
    const d = JSON.parse(_txt);
    document.getElementById('chips').innerHTML = Object.entries(d.counts)
      .map(([s,n])=>`<span class=chip>${{Z[s]||s}} <b>${{n}}</b></span>`).join('');
    const pc = document.getElementById('pendingcard');
    pc.style.display = d.pending.length ? '' : 'none';
    document.getElementById('pending').innerHTML = d.pending.map(v=>`<tr>
      <td class=dim>${{esc(v.cname)}}</td><td>${{esc((v.title||v.video_id).slice(0,60))}}</td>
      <td class=dim>${{esc(v.pub_local||'')}}</td>
      <td>${{btn('approve',v.video_id,'▶ 处理','primary')}} ${{btn('skip',v.video_id,'跳过')}}</td></tr>`).join('');
    const running = ['metadata_ready','caption_check','audio_queued',
      'awaiting_transcription','transcript_ready','article_ready',
      'visual_planned','frames_ready','package_ready','written'];
    document.getElementById('rows').innerHTML = d.rows.length ? d.rows.map(v=>{{
      const cls = v.status==='verified'?'ok'
        :(v.status==='failed'||v.status==='dead_letter')?'bad'
        :(running.includes(v.status)?'run':'');
      let act='';
      const _stuckMin = v.updated_at ? Math.round((Date.now()-Date.parse(v.updated_at))/60000) : 0;
      const _isStuck = running.includes(v.status) && _stuckMin >= 10;
      if (v.status==='failed'||v.status==='dead_letter')
        act = btn('retry',v.video_id,'重试');
      else if (v.status==='ignored') act = btn('retry',v.video_id,'↩ 取消跳过');
      else if (_isStuck) act = btn('retry',v.video_id,'重新运行');
      const skippable = ['discovered','metadata_ready','caption_check','audio_queued',
                         'awaiting_transcription','transcript_ready','article_ready'];
      if (skippable.includes(v.status))
        act += ' ' + btn('skip',v.video_id,'跳过');
      let det = v.error_code || v.last_detail || '';
      if (_isStuck && v.status !== 'awaiting_transcription') det = '⚠ 已卡在此步 ' + _stuckMin + ' 分钟，可点重新运行';
      if (v.status === 'awaiting_transcription' && v.updated_at) {{
        const mins = Math.max(0, Math.round((Date.now() - Date.parse(v.updated_at)) / 60000));
        det = `已等待 ${{mins}} 分钟（出稿后自动继续）`;
      }}
      return `<tr><td class=dim>${{esc(v.cname)}}</td>
        <td class=t title="${{esc(v.title||v.video_id)}}"><div class=clamp>${{v.status==='verified' ? `<a href="/reports/${{v.video_id}}" style="color:inherit;text-decoration:underline dotted">${{esc(v.title||v.video_id)}}</a>` : esc(v.title||v.video_id)}}</div></td>
        <td class=dim>${{esc(_dur(v.duration_sec))}}</td>
        <td class=dim>${{esc(v.pub_local||'')}}</td>
        <td><span class="st ${{cls}}">${{Z[v.status]||v.status}}</span></td>
        <td class="dim t" style="max-width:220px" title="${{esc(det)}}"><div class=clamp>${{esc(det)}}</div></td>
        <td class=dim>${{esc(v.upd_local||'')}}</td><td>${{act}}</td></tr>`;
    }}).join('') : `<tr><td colspan=8>${{document.getElementById('dd-empty').innerHTML}}</td></tr>`;
  }} catch(e) {{}}
}}
function _dur(s) {{ if(!s) return ''; const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return h? `${{h}}:${{String(m).padStart(2,'0')}}:${{String(x).padStart(2,'0')}}` : `${{m}}:${{String(x).padStart(2,'0')}}`; }}
refresh(); setInterval(refresh, 4000);
</script>"""
    con.close()
    return page("队列", "queue", body)


@app.route("/run-now-redirect")
def run_now_get():
    """approve 后自动触发一轮处理再回到队列页。"""
    from .paths import APP_SUPPORT, cli_launch_argv
    argv, cwd = cli_launch_argv("run", "--once", "--headless")
    subprocess.Popen(
        argv, cwd=cwd,
        stdout=open(APP_SUPPORT / "logs" / "manual-run.log", "ab"),
        stderr=subprocess.STDOUT, start_new_session=True)
    return redirect(url_for("queue", started=1))


# --- Reports ------------------------------------------------------------------

def _vault_root():
    return cfg_mod.load().vault_root


def _safe_vault_path(rel: str) -> Path:
    root = _vault_root()
    if root is None:
        abort(404)
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        abort(403)
    return p


def _md_to_html(text: str, video_id: str) -> str:
    import re as _re
    # frontmatter → collapsed card
    fm = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_raw = text[3:end].strip()
            text = text[end + 4:]
            fm = (f"<details><summary class=dim>frontmatter</summary>"
                  f"<pre>{escape(fm_raw)}</pre></details>")
    # ![[att]] → vault file route
    text = _re.sub(
        r"!\[\[([^\]]+)\]\]",
        lambda m: f'<img src="/vault-file?p={escape(m.group(1))}" loading=lazy>',
        text)
    # [[wikilink]] → plain highlighted text (v0.3: resolve)
    text = _re.sub(r"\[\[([^\]]+)\]\]", r"<b>\1</b>", text)
    # Obsidian foldable callout "> [!quote]- title" (+ following "> " lines)
    # → <details> so it folds in the in-app reader too
    def _callout(m):
        title = m.group(1).strip()
        inner = _re.sub(r"^> ?", "", m.group(2), flags=_re.M)
        return (f"<details><summary>{escape(title)}</summary>"
                f"<pre style='white-space:pre-wrap'>{escape(inner)}</pre></details>")
    text = _re.sub(r"^> \[!\w+\]- (.*)\n((?:^>.*\n?)*)", _callout, text,
                   flags=_re.M)
    try:
        import markdown
        html = markdown.markdown(text, extensions=["tables", "fenced_code"])
    except ImportError:
        html = "<pre style='white-space:pre-wrap'>" + str(escape(text)) + "</pre>"
    return fm + html


@app.route("/reports/delete", methods=["POST"])
def report_delete():
    check_csrf()
    from . import trash
    con = _con()
    trash.trash_article(con, cfg_mod.load(), request.form.get("video_id", ""))
    con.close()
    return redirect(url_for("reports"))


@app.route("/reports/restore", methods=["POST"])
def report_restore():
    check_csrf()
    from . import trash
    con = _con()
    trash.restore(con, request.form.get("entry", ""))
    con.close()
    return redirect(url_for("reports"))


@app.route("/trash.json")
def trash_json():
    from . import trash
    keep = cfg_mod.load().get("retention.trash_days", 3)
    trash.purge_expired(keep)  # lazily purge on view
    from flask import jsonify
    return jsonify(trash.list_trash(keep))


@app.route("/reports.json")
def reports_json():
    con = _con()
    rows = con.execute(
        "SELECT w.video_id, MAX(w.at) at, w.note_path, v.title vtitle, "
        "v.published_at, v.duration_sec, c.name cname, c.grp cgrp "
        "FROM writes w JOIN videos v USING(video_id) "
        "LEFT JOIN channels c USING(channel_id) "
        "WHERE w.note_kind='wiki' GROUP BY w.video_id ORDER BY w.at DESC").fetchall()
    import json as _json
    from .paths import work_dir
    tmap = _load_tagmap()
    hidden = _load_hidden()
    out = []
    for r in rows:
        tags = []
        companies = []
        try:
            aj = work_dir(r["video_id"]) / "article.json"
            if aj.exists():
                art_data = _json.loads(aj.read_text(encoding="utf-8"))
                tags = [t for t in _merge_tags(
                    art_data.get("tags", [])[:6], tmap)
                    if t not in hidden]
                companies = [c for c in art_data.get("companies", [])[:6]
                            if isinstance(c, str) and c.strip()]
        except Exception:
            pass
        out.append({
            "video_id": r["video_id"],
            "title": Path(r["note_path"]).stem.rsplit("--", 1)[0],
            "channel": r["cname"] or "未知频道",
            "published": dbm.local_date(r["published_at"]),
            "generated": dbm.local_date(r["at"]),
            "duration_sec": r["duration_sec"] or 0,
            "tags": tags,
            "companies": companies,
            "grps": _grps_of(r["cgrp"]) if "cgrp" in r.keys() else [],
        })
    con.close()
    from flask import jsonify
    return jsonify(out)


TAGMERGE_SYSTEM = """你是标签归并助手。给你 JSON：{"tags":[全部标签], "confirmed":{标签:用户已确认的归属}}。
把意思相同或属于同一主题家族的标签分组合并，每组选一个规范名。
规则：
- 规范名选组内最短、最通用的（例：AI、AI 技术、AI 投资 → AI；财报、财报季、财报分析 → 财报）。
- confirmed 是用户此前的人工决定，必须严格遵守：值为规范名则该标签必须归入该组；值为 "独立" 则该标签不得被合并、也不得作为你提问的对象。
- 只合并你有把握的；**对拿不准的标签不要合并，改为向用户提问**（最多 5 题）：
  每题针对一个标签，options 只能是当前标签集合里的规范名候选，外加 "独立"。
- 未出现在任何组里且未提问的标签保持原样。
只输出 JSON：
{"groups":[{"canon":"AI","alts":["AI 技术","AI 投资"]}],
 "questions":[{"tag":"AI 芯片","question":"「AI 芯片」应归入哪个标签？","options":["AI","芯片","独立"]}]}"""


def _tagmap_path():
    from .paths import APP_SUPPORT
    return APP_SUPPORT / "tags-merge.json"


def _load_tagfile() -> dict:
    try:
        import json as _j
        return _j.loads(_tagmap_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_tagmap() -> dict:
    return _load_tagfile().get("map", {})


def _apply_tag_decisions(tmap: dict, decisions: dict) -> dict:
    """用户人工决定优先于 AI：'' = 保持独立（从 map 移除）；否则强制指向所选规范名。"""
    out = {a: c for a, c in tmap.items() if decisions.get(a, None) != ""}
    for tag, canon in decisions.items():
        if canon:
            out[tag] = canon
        else:
            out.pop(tag, None)
    # 防环/防链
    return {a: c for a, c in out.items() if c not in out and a != c}


def _merge_tags(tags, tmap):
    out = []
    for t in tags or []:
        c = tmap.get(t, t)
        if c not in out:
            out.append(c)
    return out


def _article_tag_lists(con) -> list:
    """每篇文章的原始标签列表（去重后 [:6]），用于计数孤儿标签。"""
    import json as _json
    from .paths import work_dir
    rows = con.execute(
        "SELECT DISTINCT video_id FROM writes WHERE note_kind='wiki'").fetchall()
    out = []
    for r in rows:
        try:
            aj = work_dir(r["video_id"]) / "article.json"
            if aj.exists():
                ts = _json.loads(aj.read_text(encoding="utf-8")).get("tags", [])[:6]
                out.append([t for t in ts if isinstance(t, str) and t.strip()])
        except Exception:
            pass
    return out


def _all_article_tags(con, lists: list | None = None) -> list:
    """lists 可以传入已经算好的 _article_tag_lists(con) 结果，省一遍磁盘全量
    读取——tags_merge() 在同一个请求里既要算全部标签、又要（可选）算孤儿
    计数，两边共用一份，不用每次都把所有 article.json 再读一遍。"""
    tags = set()
    for lst in (lists if lists is not None else _article_tag_lists(con)):
        tags.update(lst)
    return sorted(tags)


def _canon_article_counts(con, tmap: dict, lists: list | None = None) -> dict:
    """归并后每个规范标签被多少篇文章使用（每篇去重计一次）。"""
    counts: dict = {}
    for lst in (lists if lists is not None else _article_tag_lists(con)):
        for t in {tmap.get(x, x) for x in lst}:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _load_hidden() -> set:
    h = _load_tagfile().get("hidden")
    return set(h) if isinstance(h, list) else set()


@app.post("/tags/merge")
def tags_merge():
    check_csrf()
    import json as _j
    from . import providers
    con = _con()
    article_tag_lists = _article_tag_lists(con)
    tags = _all_article_tags(con, article_tag_lists)
    con.close()
    if len(tags) < 2:
        return {"ok": True, "merged": 0, "total": len(tags)}
    decisions = _load_tagfile().get("decisions", {})
    decisions = {k: v for k, v in decisions.items() if k in set(tags)}
    try:
        payload = {"tags": tags,
                   "confirmed": {k: (v or "独立") for k, v in decisions.items()}}
        raw = providers.complete(cfg_mod.load(), None, "tag-merge",
                                 TAGMERGE_SYSTEM,
                                 _j.dumps(payload, ensure_ascii=False),
                                 max_tokens=2500, purpose="report_qa").strip()
        parsed = _j.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        groups = parsed.get("groups", [])
        questions = parsed.get("questions", [])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}, 502
    tmap = {}
    tagset = set(tags)
    for g in groups:
        canon = (g.get("canon") or "").strip()
        if not canon:
            continue
        for a in g.get("alts") or []:
            a = (a or "").strip()
            if a and a != canon and a in tagset:
                tmap[a] = canon
    tmap = _apply_tag_decisions(tmap, decisions)
    # 清洗问题：标签必须真实存在、未被人工决定过；选项限于真实标签 + 独立
    qs = []
    for q in questions if isinstance(questions, list) else []:
        t = (q.get("tag") or "").strip()
        if not t or t not in tagset or t in decisions:
            continue
        opts = [o for o in (q.get("options") or [])
                if isinstance(o, str) and (o == "独立" or o in tagset) and o != t]
        if "独立" not in opts:
            opts.append("独立")
        if len(opts) < 2:
            continue
        qs.append({"tag": t,
                   "question": (q.get("question") or
                                "「" + t + "」应归入哪个标签？")[:120],
                   "options": opts[:6]})
        if len(qs) >= 5:
            break
    # 孤儿标签清理（可选）：归并后仍只被 <= orphan_min 篇文章使用的标签，从展示层隐藏。
    hidden = []
    drop_orphans = request.form.get("drop_orphans") == "1"
    try:
        orphan_min = max(1, int(request.form.get("orphan_min", 1)))
    except (TypeError, ValueError):
        orphan_min = 1
    if drop_orphans:
        counts = _canon_article_counts(None, tmap, article_tag_lists)
        # 不隐藏：用户已人工决定过的标签、以及作为某组规范名的标签
        canon_names = set(tmap.values())
        keep = set(decisions.keys()) | canon_names
        hidden = sorted(t for t, c in counts.items()
                        if c <= orphan_min and t not in keep)
    _tagmap_path().write_text(_j.dumps(
        {"map": tmap, "decisions": decisions, "hidden": hidden,
         "orphan_min": orphan_min, "made": dbm.now(), "tags": tags},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "merged": len(tmap), "total": len(tags),
            "orphans_removed": len(hidden), "questions": qs}


@app.post("/tags/merge/answers")
def tags_merge_answers():
    check_csrf()
    import json as _j
    try:
        answers = _j.loads(request.form.get("answers", "{}"))
        assert isinstance(answers, dict)
    except Exception:
        return {"ok": False, "error": "bad answers"}, 400
    con = _con()
    tagset = set(_all_article_tags(con))
    con.close()
    data = _load_tagfile()
    tmap = data.get("map", {})
    decisions = data.get("decisions", {})
    applied = 0
    for tag, choice in list(answers.items())[:20]:
        if not isinstance(tag, str) or tag not in tagset:
            continue
        if choice == "独立" or choice == "":
            decisions[tag] = ""
            applied += 1
        elif isinstance(choice, str) and choice in tagset and choice != tag:
            decisions[tag] = choice
            applied += 1
    tmap = _apply_tag_decisions(tmap, decisions)
    # 用户确认的标签不再被当作孤儿隐藏
    hidden = [h for h in data.get("hidden", []) if h not in decisions]
    _tagmap_path().write_text(_j.dumps(
        {"map": tmap, "decisions": decisions, "hidden": hidden,
         "orphan_min": data.get("orphan_min", 1), "made": dbm.now(),
         "tags": data.get("tags", sorted(tagset))},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "applied": applied, "merged": len(tmap)}


def _digest_note_text(note_path, limit=6000):
    '''Persistent article body for the daily digest: read the vault wiki note,
    strip YAML frontmatter and image lines, truncate.'''
    from pathlib import Path as _P
    try:
        raw = _P(note_path).read_text(encoding='utf-8')
    except Exception:
        return ''
    lines = raw.split(chr(10))
    if lines and lines[0].strip() == '---':
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                end = i
                break
        if end is not None:
            lines = lines[end + 1:]
    out = [ln for ln in lines if not ln.strip().startswith('![')]
    return (chr(10).join(out)).strip()[:limit]


DIGEST_SYSTEM = """你是情报汇总编辑。给你某一天收到的多篇视频文章的完整材料
（标题/频道/完整正文，可能较长）。输出当日汇总报告（Markdown），要求：
# 当日情报汇总（{date}）
1. 总览：3-5 句概括当天信息全貌。
2. 按主题归并的详细要点：**必须覆盖材料中的每一条要点，一条都不许漏**；
   同主题合并叙述但保留各自的具体数字与结论；
   每条要点末尾用【标题】标注来源文章。
3. "值得注意"：各来源间的分歧观点、共同强调的信号、以及孤立但重要的单点信息。
只依据给定材料，不补充外部信息。宁可长，不可漏。"""


DIGEST_KEEP_DAYS = 30


def _digest_cache_path(date: str, grp: str):
    from .paths import APP_SUPPORT
    import hashlib as _h
    d = APP_SUPPORT / "digests"
    d.mkdir(parents=True, exist_ok=True)
    # 清理超过 30 天的缓存
    import time as _t
    cutoff = _t.time() - DIGEST_KEEP_DAYS * 86400
    for p in d.glob("*.md"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
    key = date + "__" + "+".join(sorted(x.strip() for x in grp.split(",") if x.strip()))
    return d / (_h.sha256(key.encode()).hexdigest()[:16] + "__" + date + ".md")


def _digest_cache_path2(date: str, grp: str, extra: str):
    p = _digest_cache_path(date, grp)
    if not extra:
        return p
    return p.with_name(extra + "__" + p.name)


def _digest_timing_path():
    from .paths import APP_SUPPORT
    return APP_SUPPORT / "digest-timing.json"


def _digest_last_sec():
    import json as _j
    try:
        return int(_j.loads(_digest_timing_path().read_text(encoding="utf-8")).get("last_sec", 0))
    except Exception:
        return 0


# 当天（全部组，未筛选）文章数一旦超过 2 篇（即 >=3），管线写完就自动在后台
# 预生成/刷新日报缓存，用户打开 Reports 时日报已经是现成的，不用现等 AI 生成。
AUTO_DIGEST_MIN_ITEMS = 3


def _digest_today_count(con, date: str) -> int:
    rows = con.execute(
        "SELECT DISTINCT w.video_id, v.published_at "
        "FROM writes w JOIN videos v USING(video_id) "
        "WHERE w.note_kind='wiki'").fetchall()
    return sum(1 for r in rows if dbm.local_date(r["published_at"]) == date)


def _digest_auto_state_path():
    from .paths import APP_SUPPORT
    return APP_SUPPORT / "digest-auto-state.json"


def maybe_autogenerate_digest(log=None):
    """管线每轮写入结束后调用（见 pipeline.run_once）。当天文章数达到
    AUTO_DIGEST_MIN_ITEMS 且比上次自动生成时更多（有新内容），就在后台
    线程里对全部组（grp=""）的默认日报调用与 /reports/digest 完全相同的
    视图逻辑强制刷新缓存——保证自动预生成的缓存 key 与用户手动打开时
    读到的完全一致，不会各算各的。已是最新则直接跳过，不浪费 AI 调用。
    返回后台线程对象（daemon，已 start）——调用方一般不需要它，测试里可以
    join() 等待完成。"""
    import threading

    def _work():
        try:
            con = _con()
            today = dbm.local_date(dbm.now())
            n = _digest_today_count(con, today)
            con.close()
            if n < AUTO_DIGEST_MIN_ITEMS:
                return
            import json as _j
            state_path = _digest_auto_state_path()
            try:
                state = _j.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
            last_n = state.get(today, 0)
            cache_exists = _digest_cache_path(today, "").exists()
            if n <= last_n and cache_exists:
                return  # 没有新增内容，缓存已是最新
            cl = app.test_client()
            r = cl.post("/reports/digest", data={
                "_csrf": CSRF, "date": today, "grp": "", "force": "1",
            })
            if r.status_code == 200:
                state[today] = n
                import datetime as _dt
                keep_after = (_dt.date.today() - _dt.timedelta(days=35)).isoformat()
                state = {k: v for k, v in state.items() if k >= keep_after}
                state_path.write_text(_j.dumps(state), encoding="utf-8")
                if log:
                    log.event("digest_autogenerated", detail=f"{today} n={n}")
            elif log:
                log.event("digest_autogen_failed", detail=f"http {r.status_code}")
        except Exception as e:
            if log:
                log.event("digest_autogen_failed", detail=str(e))

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    return t


@app.route("/reports/digest", methods=["POST"])
def reports_digest():
    check_csrf()
    date = request.form.get("date", "")[:10]
    grp = request.form.get("grp", "").strip()
    force = request.form.get("force") == "1"
    con = _con()
    rows = con.execute(
        "SELECT w.video_id, w.note_path, v.title, v.published_at, "
        "c.name cname, c.grp cgrp "
        "FROM writes w JOIN videos v USING(video_id) "
        "LEFT JOIN channels c USING(channel_id) "
        "WHERE w.note_kind='wiki' GROUP BY w.video_id").fetchall()
    import json as _json
    from .paths import work_dir
    _digest_tmap = _load_tagmap()
    _digest_hidden = _load_hidden()
    items = []
    item_grps = set()
    for r in rows:
        if dbm.local_date(r["published_at"]) != date:
            continue
        if grp:
            raw_sel = [g.strip() for g in grp.split(",")]
            allowed = {g for g in raw_sel if g}
            vg = set(_grps_of(r["cgrp"]))
            if not ((vg & allowed) or ("" in raw_sel and not vg)):
                continue
        meta = {}
        aj = work_dir(r["video_id"]) / "article.json"
        try:
            if aj.exists():
                meta = _json.loads(aj.read_text(encoding="utf-8"))
        except Exception:
            pass
        item_grps.update(_grps_of(r["cgrp"]))
        items.append({
            "vid": r["video_id"],
            "title": meta.get("title_zh") or r["title"] or r["video_id"],
            "channel": r["cname"] or "",
            "content": _digest_note_text(r["note_path"]),
            "summary": meta.get("summary", ""),
            "takeaways": meta.get("takeaways", []),
            "sections": [sec.get("heading", "")
                         for sec in meta.get("sections", [])][:12],
            "tags": [t for t in _merge_tags(
                [x for x in meta.get("tags", [])[:6]
                 if isinstance(x, str) and x.strip()], _digest_tmap)
                if t not in _digest_hidden],
        })
    con.close()
    if not items:
        body = ('<div class=card><a class=dim href="/reports">← 返回</a>'
                '<p class=dim>该日期（' + escape(date) + '）'
                + (('组「' + escape(grp) + '」') if grp else '')
                + '没有可汇总的文章。</p></div>')
        return page("日报", "reports", body)
    # 基于组的总结个性化：作用域组（未选组时=当日文章实际所属的组）
    _cfg0 = cfg_mod.load()
    _gp_map = _cfg0.get("groups.prompts") or {}
    _scope_gs = (sorted({x.strip() for x in grp.split(",") if x.strip()})
                 if grp else sorted(item_grps))
    gp_text = "\n".join(
        "【组：" + g + "】" + (_gp_map.get(g) or "").strip()
        for g in _scope_gs if (_gp_map.get(g) or "").strip())
    import hashlib as _hh
    _extra = _hh.sha1(gp_text.encode("utf-8")).hexdigest()[:8] if gp_text else ""
    cache = _digest_cache_path2(date, grp, _extra)
    cached = cache.exists() and not force
    from . import providers
    import json as _j
    if cached:
        md = cache.read_text(encoding="utf-8")
    else:
        user = _j.dumps([{k: v for k, v in it.items() if k != "vid"}
                         for it in items], ensure_ascii=False)[:60000]
        import time as _time
        _t0 = _time.time()
        try:
            _dsys = DIGEST_SYSTEM.format(date=date)
            if gp_text:
                _dsys += ("\n\n各组的个性化要求（只作用于对应组的文章；"
                          "不得虚构，不得因此遗漏要点）：\n" + gp_text)
            md = providers.complete_long(cfg_mod.load(), None, f"digest-{date}",
                                         _dsys, user,
                                         max_tokens=8000, purpose="report_qa",
                                         max_rounds=3)
            cache.write_text(md, encoding="utf-8")
            import json as _jt
            _digest_timing_path().write_text(_jt.dumps({"last_sec": int(_time.time() - _t0)}), encoding="utf-8")
        except Exception as e:
            md = f"生成失败：{e}"
    html = _md_to_html(md, "digest")
    import re as _re
    html = _re.sub(r"【([^】\n]{1,80})】",
                   r'<span class=cite>【\1】</span>', html)
    refs = "".join(
        '<li><a href="/reports/' + str(escape(it["vid"]))
        + '" style="color:var(--acc)">' + str(escape(it["title"]))
        + '</a> <span class=dim>· ' + str(escape(it["channel"])) + '</span></li>'
        for it in items)
    scope = ('组「' + str(escape(grp.replace(",", "+"))) + '」 · ') if grp else ""
    cache_note = ('<span class="st ok" style="margin-left:8px">已加载缓存</span>'
                  if cached else
                  '<span class="st run" style="margin-left:8px">新生成</span>')
    regen = (f'<form method=post action=/reports/digest style="display:inline;margin-left:8px" onsubmit="return digestSubmit(this)">'
             f'<input type=hidden name=_csrf value={CSRF}>'
             f'<input type=hidden name=date value="{escape(date)}">'
             f'<input type=hidden name=grp value="{escape(grp)}">'
             f'<input type=hidden name=force value=1>'
             f'<button style="font-size:12px;padding:2px 10px">♻ 重新生成</button></form>'
             f'<button style="font-size:12px;padding:2px 10px;margin-left:8px" id=citebtn'
             f' onclick="toggleCites()">显示引用：开</button>'
             f'<button style="font-size:12px;padding:2px 10px;margin-left:8px"'
             f' onclick="copyDigest(this)">⧉ 复制</button>')
    tag_counts = {}
    for it in items:
        for t in it.get("tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tag_html = ""
    if tag_counts:
        chips = "".join(
            '<span class="tagchip tgedit" data-tag="' + str(escape(t)) + '"'
            + ' data-n="' + str(n) + '">' + str(escape(t))
            + (' <span class=dim>×' + str(n) + '</span>' if n > 1 else '')
            + '</span>'
            for t, n in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0])))
        tag_html = ('<div class=md style="margin-top:16px"><h2>涉及标签'
                    '<span class=dim style="font-size:13px;font-weight:400">'
                    '（点一次选中、再点一次从当天全部相关文章删除）</span></h2>'
                    '<div id=digesttags style="line-height:2.2">' + chips + '</div></div>')
    body = (f'<div class=card><a class=dim href="/reports">← 返回时间轴</a>'
            f'<span class=dim style="margin-left:10px">{scope}{escape(date)}'
            f' · 共 {len(items)} 篇</span>{cache_note}{regen}'
            f'<div class=md>{html}</div>'
            f'{tag_html}'
            f'<div class=md style="margin-top:18px"><h2>引用来源</h2>'
            f'<ol>{refs}</ol></div></div>'
            + _digest_tag_js(date, grp)
            + _DIGEST_TOOLS_JS.replace("__RAWMD__", _j.dumps(md)))
    return page("日报", "reports", body)


def _digest_tag_js(date: str, grp: str) -> str:
    import json as _j
    return ("<script>(function(){"
            "var box=document.getElementById('digesttags');"
            "if(!box)return;"
            "var DATE=" + _j.dumps(date) + ",GRP=" + _j.dumps(grp) + ";"
            "function disarm(){box.querySelectorAll('.tgedit.arm').forEach(function(c){"
            "c.classList.remove('arm');c.innerHTML=c.dataset.html;});}"
            "box.querySelectorAll('.tgedit').forEach(function(c){c.dataset.html=c.innerHTML;});"
            "document.addEventListener('click',function(e){"
            "if(!e.target.closest('#digesttags .tgedit'))disarm();});"
            "box.addEventListener('click',function(e){"
            "var chip=e.target.closest('.tgedit');if(!chip)return;"
            "if(!chip.classList.contains('arm')){disarm();chip.classList.add('arm');"
            "var n=chip.dataset.n>1?('（'+chip.dataset.n+'篇）'):'';"
            "chip.textContent='\u2715 '+chip.dataset.tag+n+'（再点删除）';return;}"
            "var tag=chip.dataset.tag;chip.textContent='\u5220\u9664\u4e2d\u2026';"
            "fetch('/reports/digest/tag-remove',{method:'POST',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
            "body:'_csrf=" + CSRF + "&date='+encodeURIComponent(DATE)"
            "+'&grp='+encodeURIComponent(GRP)+'&tag='+encodeURIComponent(tag)})"
            ".then(function(r){return r.json();}).then(function(d){"
            "if(d.ok)chip.remove();else{chip.classList.remove('arm');"
            "chip.innerHTML=chip.dataset.html;}})"
            ".catch(function(){chip.classList.remove('arm');chip.innerHTML=chip.dataset.html;});"
            "});})();</script>")


_DIGEST_TOOLS_JS = """
<style>body.nocite .cite{display:none}</style>
<script>
const RAWMD = __RAWMD__;
function _syncCiteBtn() {
  const off = document.body.classList.contains('nocite');
  document.getElementById('citebtn').textContent = off ? '显示引用：关' : '显示引用：开';
}
function toggleCites() {
  document.body.classList.toggle('nocite');
  try { localStorage.setItem('ytrec-nocite',
        document.body.classList.contains('nocite') ? '1' : ''); } catch(e) {}
  _syncCiteBtn();
}
try { if (localStorage.getItem('ytrec-nocite') === '1')
        document.body.classList.add('nocite'); } catch(e) {}
_syncCiteBtn();
async function copyDigest(btn) {
  let text = RAWMD;
  if (document.body.classList.contains('nocite'))
    text = text.replace(/【[^】]{1,80}】/g, '');
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch(e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch(e2) {}
    ta.remove();
  }
  btn.textContent = ok ? '✓ 已复制' : '复制失败';
  setTimeout(() => { btn.textContent = '⧉ 复制'; }, 2500);
}
</script>"""


@app.route("/reports/bulk-delete", methods=["POST"])
def reports_bulk_delete():
    check_csrf()
    from . import trash
    ids = [i for i in request.form.get("ids", "").split(",") if i]
    con = _con()
    cfg = cfg_mod.load()
    for vid in ids[:100]:
        trash.trash_article(con, cfg, vid)
    con.close()
    return redirect(url_for("reports"))


_REPORTS_TMPL = """
<div class="card grpbar" id=grpcard style="display:none;padding:8px 14px">
<span class=dim style="margin-right:4px">组：</span><span id=grpchips></span></div>
<div class=tabs>
<button data-m=timeline class=on>📅 时间轴</button>
<button data-m=read>📖 阅读</button>
<button data-m=manage>🗂 管理</button>
</div>
<div class=card style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<input id=q placeholder="搜索标题…" style="flex:1;min-width:180px" oninput="render()">
<select id=chan onchange="render()"><option value="">全部频道</option></select>
<select id=grp onchange="render()">
<option value=date>按日期分组</option>
<option value=channel>按频道分组</option>
<option value=flat>平铺列表</option></select>
<span id=count class=dim></span></div>
<div class=card id=tagbar style="display:none;padding:10px 14px 8px;overflow:visible">
<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
<button id=tagtab-content class="tagsubtab on" onclick="setTagTab('content')">内容标签 <span id=tagtab-content-count class=dim></span></button>
<button id=tagtab-co class=tagsubtab onclick="setTagTab('companies')">🏢 公司/实体 <span id=tagtab-co-count class=dim></span></button>
</div>
<div class=tagwrap><div class=taginner>
<span id=tags></span>
</div></div>
<div id=tagbar-tools style="text-align:right;margin-top:2px">
<label class=dim style="font-size:11px;margin-right:10px;user-select:none;cursor:pointer">
<input type=checkbox id=dropOrphan> 移除孤儿标签（仅 1 篇文章用到）</label>
<button id=mtbtn
 style="font-size:11px;padding:1px 8px" onclick="mergeTags()">🏷 AI 归并同义标签</button></div></div>
<div id=list></div>
<div class=card id=trashcard style="display:none"><h3>🗑 回收站 <span class=dim>· 保留 3 天后自动清除</span></h3>
<table><thead><tr><th>标题</th><th>删除于</th><th>剩余</th><th></th></tr></thead>
<tbody id=trashrows></tbody></table></div>
<div id=dd-empty style="display:none"><div class=empty>__DOODLE__这里还什么都没有</div></div>
<script>
const CSRF_T = "__CSRF__";
let DATA = [], MODE = 'timeline';
let TAGS = new Set(), GRPS = new Set(), COMPANIES = new Set();
function esc(s) { return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]); }
function dur(s) { if(!s) return ''; const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return h? h+'小时'+m+'分' : m+'分钟'; }
function filtered() {
  const q = document.getElementById('q').value.toLowerCase();
  const ch = document.getElementById('chan').value;
  return DATA.filter(r => (!q || r.title.toLowerCase().includes(q) || r.channel.toLowerCase().includes(q))
                       && (!ch || r.channel === ch)
                       && (!GRPS.size || ((r.grps&&r.grps.length) ? r.grps.some(g=>GRPS.has(g)) : GRPS.has('')))
                       && (!TAGS.size || (r.tags||[]).some(t=>TAGS.has(t)))
                       && (!COMPANIES.size || (r.companies||[]).some(c=>COMPANIES.has(c))));
}
function tagsHtml(r) {
  return (r.tags||[]).map(t=>`<span class="tagchip ${TAGS.has(t)?'on':''}" onclick="setTag('${esc(t)}');event.stopPropagation()">${esc(t)}</span>`).join('')
    + (r.companies||[]).map(c=>`<span class="tagchip co ${COMPANIES.has(c)?'on':''}" onclick="setCompany('${esc(c)}');event.stopPropagation()">🏢 ${esc(c)}</span>`).join('');
}
function setTag(t) { TAGS.has(t) ? TAGS.delete(t) : TAGS.add(t); render(); }
function setGrp(g) { GRPS.has(g) ? GRPS.delete(g) : GRPS.add(g); render(); }
function setCompany(c) { COMPANIES.has(c) ? COMPANIES.delete(c) : COMPANIES.add(c); render(); }
function item(r) {
  return `<tr><td class=t title="${esc(r.title)}"><div class=clamp><a href="/reports/${esc(r.video_id)}" style="color:var(--acc);text-decoration:none">${esc(r.title)}</a></div>${tagsHtml(r)}</td>
  <td class=dim>${esc(r.channel)}</td><td class=dim>${esc(r.published)}</td>
  <td class=dim>${dur(r.duration_sec)}</td>
  <td><form method=post action=/reports/delete style="display:inline"
    onsubmit="return confirm('删除这篇文章？将移入回收站，保留 3 天可恢复。')">
    <input type=hidden name=_csrf value="${CSRF_T}">
    <input type=hidden name=video_id value="${esc(r.video_id)}">
    <button title="移入回收站">🗑</button></form></td></tr>`;
}
function renderRead(rows) {
  const head = '<table><tr><th>标题</th><th>频道</th><th>视频发布</th><th>时长</th><th></th></tr>';
  const grp = document.getElementById('grp').value;
  if (grp === 'flat')
    return `<div class=card>${head}${rows.map(item).join('')}</table></div>`;
  const key = grp === 'channel' ? (r=>r.channel) : (r=>r.published || r.generated);
  const groups = {};
  rows.forEach(r => { const k = key(r); (groups[k] = groups[k]||[]).push(r); });
  let keys = Object.keys(groups).sort().reverse();
  if (grp === 'channel') keys.sort();
  return keys.map(k => `<div class=card><h3>${esc(k)} <span class=dim>· ${groups[k].length} 篇</span></h3>
    ${head}${groups[k].map(item).join('')}</table></div>`).join('');
}
function renderTimeline(rows) {
  const groups = {};
  rows.forEach(r => { const k = r.published || r.generated || '未知';
    (groups[k] = groups[k]||[]).push(r); });
  const keys = Object.keys(groups).sort();   // 时间轴从左到右：旧 → 新
  if (!keys.length) return '';
  const inner = keys.map(k => `<div class=tlgroup><div class=tldate>${esc(k.slice(5) || k)}
      <button style="font-size:11px;padding:1px 7px;margin-left:4px" title="总结当天全部内容"
       onclick="digest('${esc(k)}')">🧾 总结</button></div>
    ${groups[k].map(r=>`<div class=tlcard><a href="/reports/${esc(r.video_id)}" title="${esc(r.title)}"><div class=clamp>${esc(r.title)}</div></a>
      <div class=m>${esc(r.channel)} · ${dur(r.duration_sec)}</div></div>`).join('')}
    </div>`).join('');
  return `<div class=card><div class=tlwrap><div class=tl>${inner}</div></div>
   <p class=dim style="margin:4px 0 0">← 横向滚动浏览 · 自动按视频发布日期排列 →</p></div>`;
}
function renderManage(rows) {
  const head = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
    <label><input type=checkbox onchange="document.querySelectorAll('.mgc').forEach(c=>c.checked=this.checked)"> 全选</label>
    <button onclick="bulkDelete()">🗑 删除所选（入回收站）</button>
    <span class=dim id=mgcount></span></div>
    <table><tr><th></th><th>标题</th><th>频道</th><th>发布</th><th>生成</th></tr>`;
  const body = rows.map(r=>`<tr><td><input type=checkbox class=mgc value="${esc(r.video_id)}"></td>
    <td class=t title="${esc(r.title)}"><div class=clamp>${esc(r.title)}</div></td>
    <td class=dim>${esc(r.channel)}</td><td class=dim>${esc(r.published)}</td>
    <td class=dim>${esc(r.generated)}</td></tr>`).join('');
  return `<div class=card>${head}${body}</table></div>`;
}
function digest(d) {
  const gsel = [...GRPS].join(',');
  const f = document.createElement('form'); f.method='post'; f.action='/reports/digest';
  f.innerHTML = `<input type=hidden name=_csrf value="${CSRF_T}">
    <input type=hidden name=date value="${d}">
    <input type=hidden name=grp value="${gsel}">`;
  document.body.appendChild(f); digestSubmit(f, d);
}
function bulkDelete() {
  const ids = [...document.querySelectorAll('.mgc:checked')].map(c=>c.value);
  if (!ids.length) return alert('先勾选要删除的文章');
  if (!confirm(`删除所选 ${ids.length} 篇？将移入回收站，3 天内可恢复。`)) return;
  const f = document.createElement('form'); f.method='post'; f.action='/reports/bulk-delete';
  f.innerHTML = `<input type=hidden name=_csrf value="${CSRF_T}"><input type=hidden name=ids value="${ids.join(',')}">`;
  document.body.appendChild(f); f.submit();
}
function layoutChips(wrap) {
  if (!wrap || wrap.offsetParent === null) return;   // 不可见就不算（避免 0 高度误判）
  const inner = wrap.querySelector('.taginner');
  inner.classList.remove('open');
  inner.style.maxHeight = 'none'; inner.style.overflowY = 'hidden';
  const tops = [...new Set([...inner.querySelectorAll('.tagchip')].map(c => c.offsetTop))]
    .sort((a, b) => a - b);
  const full = inner.scrollHeight;
  const coll = tops.length > 2 ? tops[2] : full;   // 折叠 = 恰好两行（第三行起始处裁切）
  const exp = Math.min(full, coll * 3);            // 悬停 = 最多三倍，再多才滚动
  wrap.style.height = coll + 'px';
  inner.style.maxHeight = coll + 'px';
  wrap.onmouseenter = () => { if (tops.length <= 2) return;
    inner.classList.add('open');
    inner.style.maxHeight = exp + 'px';
    inner.style.overflowY = full > exp ? 'auto' : 'hidden'; };
  wrap.onmouseleave = () => { inner.classList.remove('open');
    inner.style.maxHeight = coll + 'px';
    inner.style.overflowY = 'hidden'; inner.scrollTop = 0; };
}
function layoutTags() { layoutChips(document.querySelector('#tagbar .tagwrap')); }
let TAGTAB = 'content';
let ALL_TAGS = new Set(), ALL_CO = new Set();
function setTagTab(tab) {
  TAGTAB = tab;
  document.getElementById('tagtab-content').classList.toggle('on', tab === 'content');
  document.getElementById('tagtab-co').classList.toggle('on', tab === 'companies');
  document.getElementById('tagbar-tools').style.display = tab === 'content' ? '' : 'none';
  renderTagCloud();
}
function renderTagCloud() {
  if (TAGTAB === 'content') {
    document.getElementById('tags').innerHTML = [...ALL_TAGS].sort().map(t=>
      `<span class="tagchip ${TAGS.has(t)?'on':''}" onclick="setTag('${esc(t)}')">${esc(t)}</span>`).join('');
  } else {
    document.getElementById('tags').innerHTML = [...ALL_CO].sort().map(c=>
      `<span class="tagchip co ${COMPANIES.has(c)?'on':''}" onclick="setCompany('${esc(c)}')">${esc(c)}</span>`).join('');
  }
  layoutTags();
}
window.addEventListener('resize', layoutTags);
function render() {
  const rows = filtered();
  const scope = [...GRPS].join('+');
  document.getElementById('count').textContent =
    `${rows.length} 篇` + (scope ? ` · 组:${scope}` : '') +
    (TAGS.size ? ` · #${[...TAGS].join(' #')}` : '') +
    (COMPANIES.size ? ` · 🏢${[...COMPANIES].join(' 🏢')}` : '');
  // 内容标签 + 公司/实体：同一个卡片里用选项卡切换，不再各占一格
  ALL_TAGS = new Set(); DATA.forEach(r=>(r.tags||[]).forEach(t=>ALL_TAGS.add(t)));
  ALL_CO = new Set(); DATA.forEach(r=>(r.companies||[]).forEach(c=>ALL_CO.add(c)));
  const tb = document.getElementById('tagbar');
  tb.style.display = (ALL_TAGS.size || ALL_CO.size) ? '' : 'none';
  document.getElementById('tagtab-content-count').textContent =
    `(${ALL_TAGS.size}${TAGS.size ? ` · 已选 ${TAGS.size}` : ''})`;
  document.getElementById('tagtab-co-count').textContent =
    `(${ALL_CO.size}${COMPANIES.size ? ` · 已选 ${COMPANIES.size}` : ''})`;
  renderTagCloud();
  // 组胶囊（置顶排，多选）
  const gAll = [...new Set(DATA.flatMap(r=>r.grps||[]))].sort();
  const gc = document.getElementById('grpcard');
  gc.style.display = gAll.length ? '' : 'none';
  document.getElementById('grpchips').innerHTML = gAll.map(g=>
    `<span class="tagchip ${GRPS.has(g)?'on':''}" onclick="setGrp('${esc(g)}')">${esc(g)}</span>`).join('')
    + `<span class="tagchip ${GRPS.has('')?'on':''}" onclick="setGrp('')">（无组）</span>`;
  const html = MODE==='timeline' ? renderTimeline(rows)
             : MODE==='manage'   ? renderManage(rows)
             : renderRead(rows);
  document.getElementById('list').innerHTML =
    html || `<div class=card>${document.getElementById('dd-empty').innerHTML.replace('这里还什么都没有','没有匹配的文章')}</div>`;
  if (MODE === 'timeline') { requestAnimationFrame(() => { const tw = document.querySelector('.tlwrap'); if (tw) tw.scrollLeft = tw.scrollWidth; }); }
  const mg = document.getElementById('mgcount'); if (mg) mg.textContent = `共 ${rows.length} 篇`;
}
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); MODE = b.dataset.m; render();
});
async function reloadData() {
  DATA = await (await fetch('/reports.json')).json();
  render();
}
async function mergeTags() {
  const b = document.getElementById('mtbtn');
  const drop = document.getElementById('dropOrphan').checked ? '1' : '0';
  b.disabled = true; b.textContent = '归并中…（AI 分析全部标签）';
  try {
    const r = await fetch('/tags/merge', {method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'_csrf=' + CSRF_T + '&drop_orphans=' + drop});
    const d = await r.json();
    if (d.ok) {
      const orph = d.orphans_removed ? `，移除 ${d.orphans_removed} 个孤儿标签` : '';
      b.textContent = `已合并 ${d.merged} 个同义标签${orph}（共 ${d.total} 个）`;
      await reloadData();
      if (d.questions && d.questions.length) { showTagQuiz(d.questions); return; }
    }
    else b.textContent = '归并失败：' + (d.error || '');
  } catch (e) { b.textContent = '归并失败'; }
  setTimeout(() => { b.textContent = '🏷 AI 归并同义标签'; b.disabled = false; }, 5000);
}
function showTagQuiz(qs) {
  const old = document.getElementById('tagquiz'); if (old) old.remove();
  const div = document.createElement('div');
  div.id = 'tagquiz'; div.className = 'card';
  div.style.cssText = 'position:fixed;right:24px;bottom:24px;max-width:380px;' +
    'z-index:50;box-shadow:0 12px 36px rgba(0,0,0,.35);max-height:70vh;overflow-y:auto';
  let h = '<h3 style="margin-top:0">🏷 AI 想跟你确认几个标签</h3>' +
    '<p class=dim style="margin:2px 0 10px">你的选择会被记住，并让之后的归并更精准。</p>';
  qs.forEach((q, i) => {
    h += '<p style="margin:8px 0 4px"><b>' + esc(q.question) + '</b></p>';
    q.options.forEach(o => {
      h += '<label style="display:inline-block;margin:2px 10px 2px 0">' +
        '<input type=radio name=tq' + i + ' value="' + esc(o) + '"> ' + esc(o) + '</label>';
    });
  });
  h += '<p style="margin-top:12px"><button class=primary onclick="submitTagQuiz()">应用我的选择</button> ' +
    '<button id=tqskip>跳过</button></p>';
  div.innerHTML = h;
  div.dataset.qs = JSON.stringify(qs);
  document.body.appendChild(div);
  div.querySelector('#tqskip').onclick = () => div.remove();
}
async function submitTagQuiz() {
  const div = document.getElementById('tagquiz');
  const qs = JSON.parse(div.dataset.qs);
  const answers = {};
  qs.forEach((q, i) => {
    const sel = div.querySelector('input[name=tq' + i + ']:checked');
    if (sel) answers[q.tag] = sel.value;
  });
  if (!Object.keys(answers).length) { div.remove(); return; }
  try {
    const r = await fetch('/tags/merge/answers', {method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'_csrf=' + CSRF_T + '&answers=' + encodeURIComponent(JSON.stringify(answers))});
    const d = await r.json();
    const b = document.getElementById('mtbtn');
    if (d.ok) { b.textContent = '已应用 ' + d.applied + ' 条人工决定'; await reloadData(); }
    setTimeout(() => { b.textContent = '🏷 AI 归并同义标签'; b.disabled = false; }, 5000);
  } catch (e) {}
  div.remove();
}
(async () => {
  DATA = await (await fetch('/reports.json')).json();
  const chans = [...new Set(DATA.map(r=>r.channel))].sort();
  document.getElementById('chan').innerHTML =
    '<option value="">全部频道</option>' + chans.map(c=>`<option>${esc(c)}</option>`).join('');
  render();
  const trash = await (await fetch('/trash.json')).json();
  if (trash.length) {
    document.getElementById('trashcard').style.display = '';
    document.getElementById('trashrows').innerHTML = trash.map(t=>`<tr>
      <td class=dim>${esc(t.title)}</td>
      <td class=dim>${esc((t.deleted_at||'').slice(0,10))}</td>
      <td class=dim>${t.days_left} 天</td>
      <td><form method=post action=/reports/restore style="display:inline">
        <input type=hidden name=_csrf value="${CSRF_T}">
        <input type=hidden name=entry value="${esc(t.entry)}">
        <button>恢复</button></form></td></tr>`).join('');
  }
})();
</script>"""


@app.route("/reports")
def reports():
    body = (_REPORTS_TMPL
            .replace("__CSRF__", CSRF)
            .replace("__DOODLE__", DOODLES["empty"]))
    return page("报告", "reports", body)


@app.route("/reports/<video_id>")
def report_view(video_id: str):
    con = _con()
    r = con.execute(
        "SELECT note_path FROM writes WHERE video_id=? AND note_kind='wiki' "
        "ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
    con.close()
    if r is None:
        abort(404)
    p = Path(r["note_path"])
    root = _vault_root()
    if root is None or not p.exists():
        abort(404)
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        abort(403)
    html = _md_to_html(p.read_text(encoding="utf-8"), video_id)
    # 文章标签（原始 article.json，供阅读页两击删除）
    import json as _tj
    from .paths import work_dir as _wd
    _atags = []
    _acompanies = []
    try:
        _aj = _wd(video_id) / "article.json"
        if _aj.exists():
            _art_data = _tj.loads(_aj.read_text(encoding="utf-8"))
            _atags = [t for t in _art_data.get("tags", [])
                      if isinstance(t, str) and t.strip()]
            _acompanies = [c for c in _art_data.get("companies", [])
                          if isinstance(c, str) and c.strip()]
    except Exception:
        _atags = []
    tags_html = ""
    if _atags:
        chips = "".join(
            '<span class="tagchip tgedit" data-tag="' + str(escape(t)) + '">'
            + str(escape(t)) + '</span>' for t in _atags)
        tags_html = (
            '<div class=card style="padding:10px 14px"><span class=dim '
            'style="margin-right:8px">标签（点一次选中、再点一次删除）：</span>'
            '<span id=tagedit>' + chips + '</span></div>')
    if _acompanies:
        co_chips = "".join(
            '<span class="tagchip co">' + str(escape(c)) + '</span>'
            for c in _acompanies)
        tags_html += (
            '<div class=card style="padding:10px 14px"><span class=dim '
            'style="margin-right:8px">公司/实体：</span>' + co_chips + '</div>')
    answer = request.args.get("_answer", "")
    ans_html = ""
    if answer:
        try:
            import markdown as _md
            _ans_body = _md.markdown(answer, extensions=["tables", "fenced_code",
                                                         "sane_lists", "nl2br"])
        except ImportError:
            _ans_body = ("<pre style='white-space:pre-wrap'>"
                         + escape(answer) + "</pre>")
        import json as _jj
        ans_html = (
            "<div class=card><div style='display:flex;align-items:center;gap:10px'>"
            "<h3 style='margin:0'>💬 AI 回答</h3>"
            "<button id=anscopy style='font-size:12px;padding:2px 10px'"
            " onclick='copyAns(this)'>⧉ 复制</button></div>"
            "<div class=md style='max-width:100%'>" + _ans_body + "</div></div>"
            "<script>const ANS_RAW = " + _jj.dumps(answer) + ";"
            "async function copyAns(b){let ok=false;"
            "try{await navigator.clipboard.writeText(ANS_RAW);ok=true;}"
            "catch(e){const t=document.createElement('textarea');t.value=ANS_RAW;"
            "document.body.appendChild(t);t.select();"
            "try{ok=document.execCommand('copy');}catch(e2){}t.remove();}"
            "b.textContent=ok?'\u2713 \u5df2\u590d\u5236':'\u590d\u5236\u5931\u8d25';"
            "setTimeout(()=>{b.textContent='\u29c9 \u590d\u5236';},2500);}</script>")
    vid_e = escape(video_id)
    body = (f"""<div class=card style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<a class=dim href='/reports'>← 返回列表</a>
<a class=dim href='obsidian://open?path={escape(str(p))}'>在 Obsidian 打开</a>
<button onclick="reloadImgs()" title="重新加载未显示的图片">🔄 刷新图片</button>
<form method=post action="/reports/{vid_e}/regen" style="margin:0"
 onsubmit="return confirm('用当前设置重新生成这篇文章？（原文文稿不变，AI 重新总结并覆盖本篇）')">
<input type=hidden name=_csrf value={CSRF}>
<button>♻ AI 重新总结</button></form>
<span id=imghint class=dim></span></div>
{tags_html}
{ans_html}
<div class=card><form method=post action="/reports/{vid_e}/ask"
 style="display:flex;gap:8px" onsubmit="this.querySelector('button').textContent='思考中…'">
<input type=hidden name=_csrf value={CSRF}>
<input name=q placeholder="就这篇文章向 AI 提问（细节查询，基于完整原文回答）…" style="flex:1" required>
<button class=primary>提问</button></form></div>
<div class=card><div class=md>{html}</div></div>
<script>
function wireImgs() {{
  document.querySelectorAll('.md img').forEach(img => {{
    img.dataset.retries = img.dataset.retries || 0;
    img.onerror = () => {{
      const n = parseInt(img.dataset.retries || '0');
      if (n < 3) {{
        img.dataset.retries = n + 1;
        setTimeout(() => {{
          img.src = img.src.split('&r=')[0] + '&r=' + Date.now();
        }}, 800 * (n + 1));
      }} else {{ img.classList.add('imgfail'); updateHint(); }}
    }};
  }});
}}
function reloadImgs() {{
  document.querySelectorAll('.md img').forEach(img => {{
    img.classList.remove('imgfail'); img.dataset.retries = 0;
    img.src = img.src.split('&r=')[0] + '&r=' + Date.now();
  }});
  updateHint();
}}
function updateHint() {{
  const bad = document.querySelectorAll('.md img.imgfail').length;
  document.getElementById('imghint').textContent =
    bad ? `⚠ ${{bad}} 张图片未能加载，可点"刷新图片"重试` : '';
}}
wireImgs();
window.addEventListener('load', () => setTimeout(updateHint, 2500));
(function() {{
  const box = document.getElementById('tagedit');
  if (!box) return;
  function disarmAll() {{
    box.querySelectorAll('.tgedit.arm').forEach(c => {{
      c.classList.remove('arm'); c.textContent = c.dataset.tag;
    }});
  }}
  document.addEventListener('click', (e) => {{
    if (!e.target.closest('#tagedit .tgedit')) disarmAll();
  }});
  box.addEventListener('click', async (e) => {{
    const chip = e.target.closest('.tgedit');
    if (!chip) return;
    if (!chip.classList.contains('arm')) {{
      disarmAll();
      chip.classList.add('arm');
      chip.textContent = '✕ ' + chip.dataset.tag + '（再点删除）';
      return;
    }}
    const tag = chip.dataset.tag;
    chip.textContent = '删除中…';
    try {{
      const r = await fetch('/reports/{vid_e}/tag-remove', {{method:'POST',
        headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
        body:'_csrf={CSRF}&tag=' + encodeURIComponent(tag)}});
      const d = await r.json();
      if (d.ok) chip.remove();
      else {{ chip.classList.remove('arm'); chip.textContent = tag; }}
    }} catch (err) {{ chip.classList.remove('arm'); chip.textContent = tag; }}
  }});
}})();
</script>""")
    return page("阅读", "reports", body)


def _write_article_tags(video_id: str, art: dict, tags: list,
                        companies: list | None = None) -> None:
    """把新的标签（及可选的公司/实体列表）写回 article.json 并原子替换，
    同步 Obsidian 笔记 frontmatter 的 tags/companies 行——老笔记还没有
    companies 行的话，在 tags 行后面插入一行（找不到笔记或不在库内则
    安静跳过）。"""
    from .paths import work_dir
    import json as _j
    aj = work_dir(video_id) / "article.json"
    art["tags"] = tags
    if companies is not None:
        art["companies"] = companies
    tmp = aj.with_suffix(".json.tmp")
    tmp.write_text(_j.dumps(art, ensure_ascii=False), encoding="utf-8")
    tmp.replace(aj)
    try:
        con = _con()
        row = con.execute(
            "SELECT note_path FROM writes WHERE video_id=? AND note_kind='wiki' "
            "ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
        con.close()
        if row:
            from . import vault as _vault
            np = Path(row["note_path"])
            root = _vault_root()
            if root and np.exists():
                np.resolve().relative_to(root.resolve())
                import re as _re
                txt = np.read_text(encoding="utf-8")
                new_line = "tags: " + _vault._yaml_list(tags)
                txt2 = _re.sub(r"(?m)^tags: .*$", new_line, txt, count=1)
                if companies is not None:
                    comp_line = "companies: " + _vault._yaml_list(companies)
                    if _re.search(r"(?m)^companies: .*$", txt2):
                        txt2 = _re.sub(r"(?m)^companies: .*$", comp_line, txt2, count=1)
                    else:
                        txt2 = _re.sub(r"(?m)^(tags: .*)$",
                                      lambda m: m.group(1) + "\n" + comp_line,
                                      txt2, count=1)
                if txt2 != txt:
                    ntmp = np.with_suffix(".md.tmp")
                    ntmp.write_text(txt2, encoding="utf-8")
                    ntmp.replace(np)
    except Exception:
        pass


def _remove_article_tag(video_id: str, tag: str) -> bool:
    """从某篇文章的 article.json 移除标签，并同步 Obsidian 笔记 frontmatter。
    返回是否实际删除（幂等：本就没有则 False）。"""
    from .paths import work_dir
    import json as _j
    aj = work_dir(video_id) / "article.json"
    if not aj.exists():
        return False
    try:
        art = _j.loads(aj.read_text(encoding="utf-8"))
    except Exception:
        return False
    orig = art.get("tags") or []
    tags = [t for t in orig if t != tag]
    if len(tags) == len(orig):
        return False
    _write_article_tags(video_id, art, tags)
    return True


def _rename_article_tags(video_id: str, mapping: dict) -> bool:
    """按 mapping（旧标签 -> 规范标签）真正重写某篇文章的标签列表（去重保序），
    并同步 Obsidian 笔记 frontmatter——和 tags-merge.json 的显示层映射不同，
    这是直接改写源数据，用于"这些标签明显就是同一个，强制合并覆盖"的场景。
    返回是否有实际变化（幂等：没有命中 mapping 则 False）。"""
    from .paths import work_dir
    import json as _j
    aj = work_dir(video_id) / "article.json"
    if not aj.exists():
        return False
    try:
        art = _j.loads(aj.read_text(encoding="utf-8"))
    except Exception:
        return False
    orig = art.get("tags") or []
    new_tags = []
    for t in orig:
        c = mapping.get(t, t) if isinstance(t, str) else t
        if c not in new_tags:
            new_tags.append(c)
    if new_tags == orig:
        return False
    _write_article_tags(video_id, art, new_tags)
    return True


def rename_tags_everywhere(mapping: dict) -> dict:
    """对全部已写入 wiki 笔记的文章应用 mapping 重写标签（见
    _rename_article_tags）。返回 {"articles_changed": n, "video_ids": [...]}。"""
    con = _con()
    rows = con.execute(
        "SELECT DISTINCT video_id FROM writes WHERE note_kind='wiki'").fetchall()
    con.close()
    changed = []
    for r in rows:
        if _rename_article_tags(r["video_id"], mapping):
            changed.append(r["video_id"])
    return {"articles_changed": len(changed), "video_ids": changed}


def _split_article_companies(video_id: str, entity_set: set) -> bool:
    """把某篇文章 tags 里属于 entity_set（公司/股票代码/具名人物/具名产品）
    的项目挪到 companies 字段（去重保序，已有 companies 的追加合并），
    tags 只保留剩下的概念标签。返回是否有实际变化（幂等）。"""
    from .paths import work_dir
    import json as _j
    aj = work_dir(video_id) / "article.json"
    if not aj.exists():
        return False
    try:
        art = _j.loads(aj.read_text(encoding="utf-8"))
    except Exception:
        return False
    orig_tags = art.get("tags") or []
    orig_companies = art.get("companies") or []
    new_tags = [t for t in orig_tags if not (isinstance(t, str) and t in entity_set)]
    moved = [t for t in orig_tags if isinstance(t, str) and t in entity_set]
    new_companies = list(orig_companies)
    for c in moved:
        if c not in new_companies:
            new_companies.append(c)
    if new_tags == orig_tags and new_companies == orig_companies:
        return False
    _write_article_tags(video_id, art, new_tags, companies=new_companies)
    return True


def split_companies_everywhere(entity_set: set) -> dict:
    """对全部已写入 wiki 笔记的文章执行公司/实体标签拆分（见
    _split_article_companies）。返回 {"articles_changed": n, "video_ids": [...]}。"""
    con = _con()
    rows = con.execute(
        "SELECT DISTINCT video_id FROM writes WHERE note_kind='wiki'").fetchall()
    con.close()
    changed = []
    for r in rows:
        if _split_article_companies(r["video_id"], entity_set):
            changed.append(r["video_id"])
    return {"articles_changed": len(changed), "video_ids": changed}


@app.post("/reports/<video_id>/tag-remove")
def report_tag_remove(video_id: str):
    check_csrf()
    tag = (request.form.get("tag") or "").strip()
    if not tag:
        return {"ok": False, "error": "empty tag"}, 400
    from .paths import work_dir
    if not (work_dir(video_id) / "article.json").exists():
        return {"ok": False, "error": "no article"}, 404
    removed = _remove_article_tag(video_id, tag)
    import json as _j
    art = _j.loads((work_dir(video_id) / "article.json").read_text(encoding="utf-8"))
    return {"ok": True, "removed": 1 if removed else 0, "tags": art.get("tags", [])}


@app.post("/reports/digest/tag-remove")
def digest_tag_remove():
    """从当天该范围内所有含此标签的文章移除标签（日报「涉及标签」两击删除）。"""
    check_csrf()
    tag = (request.form.get("tag") or "").strip()
    date = (request.form.get("date") or "")[:10]
    grp = (request.form.get("grp") or "").strip()
    if not tag or not date:
        return {"ok": False, "error": "missing tag/date"}, 400
    con = _con()
    rows = con.execute(
        "SELECT w.video_id, v.published_at, c.grp cgrp FROM writes w "
        "JOIN videos v USING(video_id) "
        "LEFT JOIN channels c USING(channel_id) "
        "WHERE w.note_kind='wiki'").fetchall()
    targets = []
    for r in rows:
        if dbm.local_date(r["published_at"]) != date:
            continue
        if grp:
            raw_sel = [g.strip() for g in grp.split(",")]
            allowed = {g for g in raw_sel if g}
            vg = set(_grps_of(r["cgrp"]))
            if not ((vg & allowed) or ("" in raw_sel and not vg)):
                continue
        targets.append(r["video_id"])
    con.close()
    removed = sum(1 for vid in targets if _remove_article_tag(vid, tag))
    # 让缓存日报重算涉及标签（下次打开即最新）
    return {"ok": True, "removed": removed, "articles": len(targets)}


@app.route("/reports/<video_id>/ask", methods=["POST"])
def report_ask(video_id: str):
    check_csrf()
    q = request.form.get("q", "").strip()
    if not q:
        return redirect(url_for("report_view", video_id=video_id))
    con = _con()
    from . import providers, transcript as tr
    can_art = dbm.get_artifact(con, video_id, "transcript_canonical")
    if can_art is None:
        con.close()
        abort(404)
    can = tr.Canonical.from_json(Path(can_art["path"]).read_text(encoding="utf-8"))
    system = ("你是视频内容问答助手。用户就一个视频的完整口述文稿提问。"
              "只依据文稿内容回答，答案中的数字与结论必须来自文稿；"
              "文稿中没有的信息明确说明'视频中未提及'。回答用中文，简明扼要。")
    user = ("问题：" + q + chr(10) * 2 + "完整文稿：" + chr(10)
            + can.full_text[:60000])
    try:
        answer = providers.complete(cfg_mod.load(), con, video_id,
                                    system, user, max_tokens=2000,
                                    purpose="report_qa")
    except Exception as e:
        answer = f"AI 调用失败：{e}"
    con.close()
    return redirect(url_for("report_view", video_id=video_id, _answer=answer[:4000]))


@app.route("/reports/<video_id>/regen", methods=["POST"])
def report_regen(video_id: str):
    check_csrf()
    con = _con()
    v = dbm.get_video(con, video_id)
    if v is None:
        con.close()
        abort(404)
    from .paths import work_dir
    art = work_dir(video_id) / "article.json"
    if art.exists():
        art.unlink()
    con.execute("DELETE FROM artifacts WHERE video_id=? AND kind='article_json'",
                (video_id,))
    con.commit()
    try:
        if v["status"] == st.VERIFIED:
            dbm.set_status(con, video_id, st.TRANSCRIPT_READY)
    except st.TransitionError:
        pass
    con.close()
    return redirect(url_for("run_now_get"))


@app.route("/vault-file")
def vault_file():
    rel = request.args.get("p", "")
    if ".." in rel or rel.startswith("/"):
        abort(403)
    p = _safe_vault_path(rel)
    if not p.exists() or p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        abort(404)
    resp = send_file(p)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# --- Settings -----------------------------------------------------------------

_KEY_CACHE: dict = {}


def _key_status(name: str) -> bool:
    """security 子进程较贵：结果缓存 60 秒，保存密钥时失效。"""
    import time
    hit = _KEY_CACHE.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        r = subprocess.run(["security", "find-generic-password", "-s",
                            f"ytrec-{name}"], capture_output=True)
        ok = r.returncode == 0
    except OSError:
        ok = False  # non-macOS (tests)
    _KEY_CACHE[name] = (time.time(), ok)
    return ok


@app.route('/api', methods=['GET', 'POST'])
def api_page():
    cfg = cfg_mod.load()
    esc = lambda v: str(escape(v))
    msg = ''
    mres = request.args.get('models', '')
    if mres.startswith('err:'):
        msg = '<span class=bad>拉取模型列表部分失败：' + esc(mres[4:]) + '</span>'
    elif mres:
        msg = '<span class=ok>模型列表已刷新（共 ' + esc(mres) + ' 个）</span>'
    if request.method == 'POST':
        check_csrf()
        f = request.form
        if f.get('form') == 'ai':
            for grp in ('article', 'visuals', 'qa'):
                v = f.get('ai_' + grp)
                if v in ('auto', 'openai', 'anthropic', 'claude_cli', 'ollama', 'qwen', 'kimi'):
                    cfg.data.setdefault('ai', {})[grp] = v
            for prov in ('openai', 'anthropic', 'claude_cli', 'ollama', 'qwen', 'kimi'):
                mv = f.get('model_' + prov, '').strip()
                if mv and len(mv) < 80:
                    cfg.data.setdefault('article', {})['model_' + prov] = mv
            try:
                cfg_mod.save(cfg); msg = '<span class=ok>AI 分工已保存</span>'
            except cfg_mod.ConfigError as e:
                msg = '<span class=bad>' + esc(str(e)) + '</span>'
            cfg = cfg_mod.load()
        elif f.get('form') == 'asr':
            cfg.data.setdefault('transcription', {})['audio_base_url'] = f.get('audio_base_url', '').strip()
            cfg.data['transcription']['audio_key'] = (f.get('audio_key', 'openai').strip() or 'openai')
            _am = f.get('api_model', '').strip()
            if _am:
                cfg.data['transcription']['api_model'] = _am
            try:
                cfg_mod.save(cfg); msg = '<span class=ok>语音识别接口已保存</span>'
            except cfg_mod.ConfigError as e:
                msg = '<span class=bad>' + esc(str(e)) + '</span>'
            cfg = cfg_mod.load()
        elif f.get('form') == 'gprompts':
            gpm = dict(cfg.get('groups.prompts') or {})
            i = 0
            while f.get('gname_' + str(i)) is not None:
                g = f.get('gname_' + str(i), '').strip()
                v = f.get('gp_' + str(i), '').strip()[:2000]
                if g:
                    if v:
                        gpm[g] = v
                    else:
                        gpm.pop(g, None)
                i += 1
            cfg.data.setdefault('groups', {})['prompts'] = gpm
            try:
                cfg_mod.save(cfg); msg = '<span class=ok>组个性化已保存</span>'
            except cfg_mod.ConfigError as e:
                msg = '<span class=bad>' + esc(str(e)) + '</span>'
            cfg = cfg_mod.load()
        elif f.get('form') == 'keys':
            for prov in ('openai', 'anthropic', 'qwen', 'kimi', 'siliconflow'):
                val = f.get('key_' + prov, '').strip()
                if val:
                    subprocess.run(['security', 'delete-generic-password', '-s', 'ytrec-' + prov], capture_output=True)
                    r = subprocess.run(['security', 'add-generic-password', '-s', 'ytrec-' + prov, '-a', 'ytrec', '-w', val], capture_output=True, text=True)
                    _KEY_CACHE.pop(prov, None)
                    msg += ('<span class=ok>' + prov + ' key 已存入钥匙串</span> ' if r.returncode == 0 else '<span class=bad>' + prov + ' 保存失败</span> ')
    dsel = lambda v, cur: 'selected' if v == cur else ''
    def _mopts(prov, fallback):
        _d = {'openai': 'gpt-4o-mini', 'anthropic': 'claude-sonnet-5', 'claude_cli': 'sonnet', 'ollama': 'llama3.1', 'qwen': 'qwen-plus', 'kimi': 'moonshot-v1-32k'}
        cur = cfg.get('article.model_' + prov, _d.get(prov, ''))
        models = _load_models().get(prov) or fallback
        if cur not in models:
            models = [cur] + models
        return ''.join('<option ' + ('selected' if m == cur else '') + '>' + esc(m) + '</option>' for m in models)
    _provs = [('auto', '自动（用已配置的，优先 OpenAI）'), ('openai', 'OpenAI'), ('anthropic', 'Anthropic (Claude)'), ('qwen', 'Qwen 通义千问'), ('kimi', 'Kimi 月之暗面'), ('claude_cli', 'Claude Code CLI（订阅额度，免 API 费）'), ('ollama', 'Ollama（本地，免费离线）')]
    def _sel(grp):
        cur = cfg.get('ai.' + grp, 'auto')
        return ''.join('<option value=' + pv + ' ' + dsel(pv, cur) + '>' + esc(lb) + '</option>' for pv, lb in _provs)
    def _krow(prov, label):
        st = '<span class=ok>已配置</span>' if _key_status(prov) else '<span class=dim>未配置</span>'
        return '<p>' + label + ' ' + st + '<br><input type=password name=key_' + prov + ' placeholder=' + chr(39) + prov + ' key（留空=不变）' + chr(39) + ' style=width:60%></p>'
    _fb = {'openai': ['gpt-4o-mini', 'gpt-4o'], 'anthropic': ['claude-sonnet-5', 'claude-haiku-4-5'], 'qwen': ['qwen-plus', 'qwen-max', 'qwen-turbo'], 'kimi': ['moonshot-v1-32k', 'moonshot-v1-8k', 'moonshot-v1-128k'], 'claude_cli': ['sonnet', 'opus', 'haiku'], 'ollama': ['llama3.1']}
    from .providers import _claude_cli_path, claude_cli_proxy_issue
    cli_ok = bool(_claude_cli_path()); cli_warn = claude_cli_proxy_issue() if cli_ok else None
    oll_ok = _ollama_alive()
    a_bu = esc(cfg.get('transcription.audio_base_url', '') or '')
    a_key = esc(cfg.get('transcription.audio_key', 'openai') or 'openai')
    a_model = esc(cfg.get('transcription.api_model', 'whisper-1') or 'whisper-1')
    p = []
    p.append('<div class=card><h3>API · 凭证与分工</h3>')
    if msg:
        p.append('<p>' + msg + '</p>')
    p.append('<p class=dim>密钥写入 macOS 钥匙串，不经过配置文件。支持 OpenAI / Anthropic / Qwen 通义千问 / Kimi 月之暗面 云端 API，以及本机 Claude Code CLI、Ollama。可给每个环节分别指定渠道，选本机渠道失败时自动回落到已配置的 API。</p>')
    p.append('<form method=post style=margin-bottom:14px><input type=hidden name=_csrf value=' + CSRF + '><input type=hidden name=form value=ai><table class=wrap>')
    p.append('<tr><td>整理成文用</td><td><select name=ai_article>' + _sel('article') + '</select></td></tr>')
    p.append('<tr><td>截图召回用</td><td><select name=ai_visuals>' + _sel('visuals') + '</select></td></tr>')
    p.append('<tr><td>问 AI 用</td><td><select name=ai_qa>' + _sel('qa') + '</select></td></tr>')
    for pv, lb in [('openai', 'OpenAI 模型'), ('anthropic', 'Anthropic 模型'), ('qwen', 'Qwen 模型'), ('kimi', 'Kimi 模型'), ('claude_cli', 'Claude Code 模型'), ('ollama', 'Ollama 模型')]:
        p.append('<tr><td>' + lb + '</td><td><select name=model_' + pv + ' style=min-width:60%>' + _mopts(pv, _fb[pv]) + '</select></td></tr>')
    p.append('<tr><td>模型列表</td><td><button formaction=/ai/models formmethod=post>🔄 刷新模型列表</button></td></tr>')
    p.append('</table><p><button class=primary>保存分工</button></p></form>')
    p.append('<form method=post><input type=hidden name=_csrf value=' + CSRF + '><input type=hidden name=form value=keys>')
    p.append(_krow('openai', 'OpenAI') + _krow('anthropic', 'Anthropic') + _krow('qwen', 'Qwen 通义千问') + _krow('kimi', 'Kimi 月之暗面') + _krow('siliconflow', 'SiliconFlow（中文语音识别）'))
    p.append('<button>保存密钥</button></form>')
    p.append('<p class=dim>本机渠道：Claude Code CLI ' + ('<span class=bad>代理未运行</span>' if cli_warn else ('<span class=ok>已检测</span>' if cli_ok else '<span class=dim>未安装</span>')) + ' · Ollama ' + ('<span class=ok>运行中</span>' if oll_ok else '<span class=dim>未运行</span>') + '</p></div>')
    p.append('<div class=card><h3>语音识别 · 转录接口</h3><p class=dim>默认走 MacWhisper 或 OpenAI Whisper。也可指向任意 OpenAI 兼容的转录接口（如 SiliconFlow 的 SenseVoice 做中文识别）：填 base_url + 选用哪个密钥 + 模型；base_url 留空即用 OpenAI 官方。注意：只有会返回分段时间码的接口才能得到精确字幕时间轴。</p>')
    p.append('<form method=post><input type=hidden name=_csrf value=' + CSRF + '><input type=hidden name=form value=asr><table class=wrap>')
    p.append('<tr><td>接口 base_url</td><td><input name=audio_base_url value=' + chr(39) + a_bu + chr(39) + ' placeholder=https://api.siliconflow.cn/v1 style=width:80%></td></tr>')
    p.append('<tr><td>用哪个密钥</td><td><input name=audio_key value=' + chr(39) + a_key + chr(39) + ' style=width:40%> <span class=dim>对应钥匙串 ytrec-该名，如 openai / siliconflow</span></td></tr>')
    p.append('<tr><td>转录模型</td><td><input name=api_model value=' + chr(39) + a_model + chr(39) + ' style=width:60%> <span class=dim>如 whisper-1 或 FunAudioLLM/SenseVoiceSmall</span></td></tr>')
    p.append('</table><p><button class=primary>保存转录接口</button></p></form></div>')
    # 基于组的总结和改写个性化
    con_g = _con()
    _all_gs = sorted({x.strip() for (row,) in con_g.execute(
        "SELECT grp FROM channels WHERE grp IS NOT NULL AND grp!=''").fetchall()
        for x in (row or "").split(",") if x.strip()})
    con_g.close()
    _gpm = cfg.get('groups.prompts') or {}
    p.append('<div class=card><h3>基于组的总结和改写个性化</h3>')
    p.append('<p class=dim>给每个组一条 prompt：该组频道的<b>单篇改写</b>和含该组文章的'
             '<b>当日汇总</b>生成时都会注入，并标注来源组（如【组：投资】）。'
             '忠实性规则（不编造事实、不因此漏要点）始终优先；'
             '留空 = 该组无个性化。改动后旧日报缓存自动失效、按需重新生成。</p>')
    if not _all_gs:
        p.append('<p class=dim>还没有任何组——先在 Channels 页给频道分组。</p></div>')
    else:
        p.append('<form method=post><input type=hidden name=_csrf value=' + CSRF
                 + '><input type=hidden name=form value=gprompts>')
        for i, g in enumerate(_all_gs):
            p.append('<p><b>' + esc(g) + '</b><input type=hidden name=gname_'
                     + str(i) + ' value=' + chr(39) + esc(g) + chr(39) + '><br>'
                     '<textarea name=gp_' + str(i) + ' rows=2 style="width:96%" '
                     'placeholder="例：偏重政策与宏观影响；结尾给出对该主题的跟踪建议">'
                     + esc(_gpm.get(g, '')) + '</textarea></p>')
        p.append('<button class=primary>保存组个性化</button></form></div>')
    return page('API', 'api', ''.join(p))


# --- Company Dossier (公司档案插件，默认关闭，见 config.dossier.enabled) --------

CATEGORY_LABELS = {"entity": "公司/实体", "index_etf": "指数/ETF"}
LEVEL_TYPE_LABELS_ZH = {"support": "支撑位", "resistance": "压力位",
                        "target": "目标价", "stop_loss": "止损位",
                        "entry": "买入/加仓", "exit": "卖出/止盈",
                        "other": "点位"}


def _dossier_note_to_html(text: str) -> str:
    """跟通用的 _md_to_html 不同：[[标题--videoId]] 这种带 videoId 后缀的
    wikilink 会渲成真的可点击链接，跳回 /reports/<videoId> 阅读原文，而不
    是只加粗成纯文本。"""
    import re as _re
    fm = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_raw = text[3:end].strip()
            text = text[end + 4:]
            fm = (f"<details><summary class=dim>frontmatter</summary>"
                  f"<pre>{escape(fm_raw)}</pre></details>")

    def _link(m):
        stem = m.group(1)
        title, vid = (stem.rsplit("--", 1) if "--" in stem else (stem, None))
        title_esc = escape(title)
        if vid:
            return (f'<a href="/reports/{escape(vid)}" target="_blank" '
                    f'style="color:var(--acc)">{title_esc}</a>')
        return f"<b>{title_esc}</b>"

    text = _re.sub(r"\[\[([^\]]+)\]\]", _link, text)
    try:
        import markdown
        html = markdown.markdown(text, extensions=["tables", "fenced_code"])
    except ImportError:
        html = "<pre style='white-space:pre-wrap'>" + str(escape(text)) + "</pre>"
    return fm + html


_DOSSIER_NOTE_META_CACHE: dict[str, tuple[float, int, dict]] = {}


def _dossier_note_meta(p: Path) -> dict | None:
    """解析笔记文件开头的 company/updated 字段 + 条目数（读全文才能数
    "\\n- [" 出现次数）。按 (mtime, size) 做内存缓存——公司档案页面
    经常被反复打开，但笔记文件通常只在跑完一轮扫描后才会变化，绝大多数
    请求应该完全不用碰磁盘。"""
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    cached = _DOSSIER_NOTE_META_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        head = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m_company = re.search(r'(?m)^company: "?([^"\n]*)"?', head)
    m_updated = re.search(r"(?m)^updated: (.*)$", head)
    item = {
        "name": (m_company.group(1) if m_company else p.stem).strip(),
        "stem": p.stem,
        "updated": (m_updated.group(1) if m_updated else "").strip(),
        "entries": head.count("\n- ["),
    }
    _DOSSIER_NOTE_META_CACHE[key] = (st.st_mtime, st.st_size, item)
    return item


@app.route("/companies")
def companies_list():
    cfg = cfg_mod.load()
    root = cfg.vault_root
    pinned_rows: list[dict] = []
    rows: list[dict] = []
    collapsed_rows: list[dict] = []
    pending_rows: list[dict] = []
    if root and cfg.get("dossier.enabled", False):
        from . import dossier as _dossier
        con = dbm.connect()
        entity_rows = {r["name"]: r for r in con.execute(
            "SELECT name, status, category, pinned, pin_order, collapsed "
            "FROM dossier_entities WHERE canonical IS NULL")}
        con.close()
        d = _dossier.dossier_dir(root)
        for p in sorted(d.glob("*.md")):
            if p.parent != d:            # 跳过 _归档 子文件夹
                continue
            meta = _dossier_note_meta(p)
            if meta is None:
                continue
            name = meta["name"]
            ent = entity_rows.get(name)
            status = ent["status"] if ent else "approved"
            category = ent["category"] if ent else "entity"
            pinned = bool(ent["pinned"]) if ent else False
            pin_order = ent["pin_order"] if ent else 0
            collapsed = bool(ent["collapsed"]) if ent else False
            item = {"name": name, "stem": meta["stem"], "updated": meta["updated"],
                    "entries": meta["entries"], "category": category,
                    "pinned": pinned, "pin_order": pin_order}
            if status == "pending":
                pending_rows.append(item)
            elif status != "rejected":
                if pinned:
                    pinned_rows.append(item)
                elif collapsed:
                    collapsed_rows.append(item)
                else:
                    rows.append(item)
    pinned_rows.sort(key=lambda r: r["pin_order"])
    rows.sort(key=lambda r: r["updated"], reverse=True)
    collapsed_rows.sort(key=lambda r: r["updated"], reverse=True)
    pending_rows.sort(key=lambda r: r["updated"], reverse=True)

    if not root:
        return page("公司档案", "companies",
                    "<div class=card>还没配置保存根目录（Settings → ⑤ 阅读与保存）。</div>")
    if not cfg.get("dossier.enabled", False):
        return page("公司档案", "companies",
                    '<div class=card>公司档案插件还没开启，去 <a href=/settings>设置</a> 里打开。</div>')

    pending_html = ""
    if pending_rows:
        items = "".join(
            f'<tr><td>{escape(r["name"])}</td>'
            f'<td class=dim>{r["entries"]} 条{"（还没建档）" if not r["entries"] else ""}</td>'
            f'<td style="display:flex;gap:8px">'
            f'<form method=post action=/companies/approve style="display:inline">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=name value="{escape(r["name"])}">'
            f'<button class=primary style="padding:2px 10px">添加</button></form>'
            f'<form method=post action=/companies/reject style="display:inline">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=name value="{escape(r["name"])}">'
            f'<button style="padding:2px 10px">忽略</button></form></td></tr>'
            for r in pending_rows)
        pending_html = (
            f"<details style='margin-bottom:10px'>"
            f"<summary class=dim>检测到 {len(pending_rows)} 个新实体，点开确认要不要建档</summary>"
            f"<div class=card><table><tr><th>名字</th><th>已抽取</th><th>操作</th></tr>"
            f"{items}</table></div></details>")

    def _row_html(r, *, pinned: bool) -> str:
        pin_btn = (
            f'<form method=post action=/companies/unpin style="display:inline" title="取消置顶">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=name value="{escape(r["name"])}">'
            f'<button style="padding:0px 7px">－</button></form>'
            if pinned else
            f'<form method=post action=/companies/pin style="display:inline" title="置顶">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=name value="{escape(r["name"])}">'
            f'<button style="padding:0px 7px">＋</button></form>')
        collapse_btn = (
            f'<form method=post action=/companies/collapse style="display:inline" title="折叠">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=name value="{escape(r["name"])}">'
            f'<button style="padding:0px 7px">折叠</button></form>')
        return (
            f'<tr><td><a href="/companies/{escape(r["stem"])}" '
            f'style="color:var(--acc);text-decoration:none">{escape(r["name"])}</a>'
            f'<span class=dim style="margin-left:6px">'
            f'{CATEGORY_LABELS.get(r["category"], "")}</span></td>'
            f'<td class=dim>{r["entries"]} 条</td>'
            f'<td class=dim>{escape(r["updated"])}</td>'
            f'<td style="display:flex;gap:4px">{pin_btn}{collapse_btn}</td></tr>')

    pinned_html = ""
    if pinned_rows:
        items = "".join(_row_html(r, pinned=True) for r in pinned_rows)
        pinned_html = (
            f"<div class=card><h3>📌 置顶</h3><table><tr><th>公司/实体</th>"
            f"<th>记录数</th><th>最近更新</th><th></th></tr>{items}</table></div>")

    collapsed_html = ""
    if collapsed_rows:
        items = "".join(
            f'<tr><td>{escape(r["name"])}</td><td class=dim>{r["entries"]} 条</td>'
            f'<td style="display:flex;gap:8px">'
            f'<a href="/companies/{escape(r["stem"])}" class=dim>打开</a>'
            f'<form method=post action=/companies/uncollapse style="display:inline">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=name value="{escape(r["name"])}">'
            f'<button style="padding:1px 8px">展开</button></form></td></tr>'
            for r in collapsed_rows)
        collapsed_html = (
            f"<details style='margin-bottom:10px'>"
            f"<summary class=dim>已折叠 {len(collapsed_rows)} 个</summary>"
            f"<div class=card><table><tr><th>名字</th><th>记录数</th><th>操作</th></tr>"
            f"{items}</table></div></details>")

    if not rows and not pinned_rows:
        body = pending_html + pinned_html + (
            "<div class=card>还没有公司档案——文章里出现公司/实体标签后，"
            "插件会在后台自动建档，写完一轮处理后回来看看。</div>") + collapsed_html
    else:
        items = "".join(_row_html(r, pinned=False) for r in rows)
        table_html = (
            f"<div class=card><table><tr><th>公司/实体</th><th>记录数</th>"
            f"<th>最近更新</th><th></th></tr>{items}</table></div>"
            if rows else "")
        body = pending_html + pinned_html + table_html + collapsed_html
    return page("公司档案", "companies", body)


@app.route("/companies/approve", methods=["POST"])
def companies_approve():
    check_csrf()
    name = request.form.get("name", "").strip()
    if name:
        con = dbm.connect()
        dbm.dossier_set_entity_status(con, name, "approved")
        con.close()
    return redirect(url_for("companies_list"))


@app.route("/companies/reject", methods=["POST"])
def companies_reject():
    check_csrf()
    name = request.form.get("name", "").strip()
    if name:
        con = dbm.connect()
        dbm.dossier_set_entity_status(con, name, "rejected")
        con.close()
        cfg = cfg_mod.load()
        root = cfg.vault_root
        if root:
            from . import dossier as _dossier
            _dossier.archive_entity_note(root, name)   # 不删除，移进归档
    return redirect(url_for("companies_list"))


@app.route("/companies/pin", methods=["POST"])
def companies_pin():
    check_csrf()
    name = request.form.get("name", "").strip()
    if name:
        con = dbm.connect()
        dbm.dossier_set_pinned(con, name, True)
        con.close()
    return redirect(url_for("companies_list"))


@app.route("/companies/unpin", methods=["POST"])
def companies_unpin():
    check_csrf()
    name = request.form.get("name", "").strip()
    if name:
        con = dbm.connect()
        dbm.dossier_set_pinned(con, name, False)
        con.close()
    return redirect(url_for("companies_list"))


@app.route("/companies/collapse", methods=["POST"])
def companies_collapse():
    check_csrf()
    name = request.form.get("name", "").strip()
    if name:
        con = dbm.connect()
        dbm.dossier_set_collapsed(con, name, True)
        con.close()
    return redirect(url_for("companies_list"))


@app.route("/companies/uncollapse", methods=["POST"])
def companies_uncollapse():
    check_csrf()
    name = request.form.get("name", "").strip()
    if name:
        con = dbm.connect()
        dbm.dossier_set_collapsed(con, name, False)
        con.close()
    return redirect(url_for("companies_list"))


@app.route("/companies/<name>")
def company_view(name: str):
    cfg = cfg_mod.load()
    root = cfg.vault_root
    if not root:
        abort(404)
    from . import dossier as _dossier
    con = dbm.connect()
    ent = dbm.dossier_get_entity(con, name)
    if ent is not None and ent["canonical"]:          # 别名 -> 跳转到规范名
        con.close()
        return redirect(url_for("company_view", name=ent["canonical"]))
    path = _dossier.dossier_note_path(root, name)
    if not path.exists():
        con.close()
        abort(404)
    html = _dossier_note_to_html(path.read_text(encoding="utf-8"))
    raw_levels = dbm.dossier_price_levels_for(con, name)
    ticker = _dossier.resolve_ticker(cfg, con, name)
    history = _dossier.fetch_price_history(ticker) if ticker else []
    summary_row = dbm.dossier_get_summary(con, name)
    pinned = bool(ent["pinned"]) if ent is not None else False
    con.close()

    reference = history[-1]["close"] if history else None
    levels, excluded = _dossier.filter_price_level_outliers(raw_levels, reference)

    chart_html = _dossier_chart_html(name, ticker, history, levels)
    _val = _dossier.fetch_valuation(ticker) if ticker else None
    _pe_hist = (_dossier.fetch_pe_history(ticker, history,
                                          (_val or {}).get("trailing_pe"))
                if ticker and history else [])
    # 这个标的本身就是指数/板块（标普500、纳指100、SOXX…）时，直接画它
    # 真正的历史动态市盈率曲线——这条线雅虎给不了，来自 historyofmarket。
    _own_bench = _dossier.fetch_benchmark_forward_pe(ticker) if ticker else {}
    valuation_html = _dossier_valuation_html(
        ticker, _val, history[-1]["close"] if history else None, _pe_hist,
        _own_bench)
    benchmark_html = _dossier_benchmark_html(
        ticker, (_val or {}).get("forward_pe"), _own_bench)

    outlier_note = ""
    if excluded:
        outlier_note = (
            f'<div class=card class=dim>已自动隐藏 {len(excluded)} 条明显跑偏的点位'
            f'（价格与参考价相差 20 倍以上，多半是抽取时张冠李戴），'
            f'数据库里还留着，不影响之后重扫。</div>')

    levels_html = ""
    if levels:
        lrows = "".join(
            f'<tr data-date="{escape(r["mentioned_date"] or "")}">'
            f'<td><form method=post action="/companies/{escape(name)}/price-level/delete" '
            f'style="display:inline">'
            f'<input type=hidden name=_csrf value="{CSRF}">'
            f'<input type=hidden name=level_id value="{r["id"]}">'
            f'<button style="padding:1px 8px" title="删除">✕</button></form></td>'
            f'<td class=dim>{escape(r["mentioned_date"] or "")}</td>'
            f'<td class=dim>{escape(r["channel"] or "")}</td>'
            f'<td>{escape(LEVEL_TYPE_LABELS_ZH.get(r["level_type"], r["level_type"] or ""))}</td>'
            f'<td>{r["price"] if r["price"] is not None else "-"}</td>'
            f'<td>{escape(r["raw_text"])}</td></tr>'
            for r in reversed(levels))
        levels_html = (
            f'<div class=card><h3>推荐点位一览</h3><table id="priceLevelTable">'
            f"<tr><th></th><th>日期</th><th>频道</th><th>类型</th><th>价格</th><th>原文</th></tr>"
            f"{lrows}</table></div>")

    summary_html = _dossier_summary_html(name, summary_row, pinned)

    body = f"""<div class=card style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<a class=dim href='/companies'>← 返回公司列表</a>
<a class=dim href='obsidian://open?path={escape(str(path))}'>在 Obsidian 打开</a></div>
{summary_html}
{valuation_html}
{benchmark_html}
{chart_html}
{outlier_note}
{levels_html}
<div class=card><div class=md>{html}</div></div>"""
    return page(name, "companies", body)


_BENCH_COMPARE = ["^GSPC", "^NDX", "SOXX"]


def _val_cell(label, value, hint):
    v = value if value is not None else "—"
    return (f'<div style="min-width:120px">'
            f'<div class=dim style="font-size:.8em;white-space:nowrap">{label}</div>'
            f'<div style="font-size:1.45em;font-weight:600">{v}</div>'
            f'<div class=dim style="font-size:.75em;white-space:nowrap">{hint}</div>'
            f'</div>')


def _dossier_benchmark_html(ticker: str | None, forward_pe: float | None,
                            own_bench: dict) -> str:
    """把这家公司的动态市盈率放到市场基准里去看。

    这是整件事的重点：单看"动态市盈率 31 倍"没法判断贵贱，但知道同期
    标普500 是 20.1 倍、费城半导体是 21.7 倍，而且费半自己正处在 2011
    年以来的 80% 分位，这才构成一个判断。

    如果这个标的本身就是指数（own_bench 非空），就不做对比——它的历史
    曲线已经画在估值卡片里了。"""
    from . import dossier as _dossier
    if not ticker or forward_pe is None or own_bench:
        return ""
    rows = []
    for bt in _BENCH_COMPARE:
        b = _dossier.fetch_benchmark_forward_pe(bt)
        if not b or not b.get("current"):
            continue
        st = _dossier.benchmark_percentile(b["series"])
        prem = (forward_pe / b["current"] - 1) * 100
        color = "#d16060" if prem > 0 else "#9ece6a"
        word = "溢价" if prem > 0 else "折价"
        rows.append(
            f'<tr><td>{escape(b["label"])}</td>'
            f'<td>{b["current"]}</td>'
            f'<td style="color:{color}">{word} {abs(prem):.0f}%</td>'
            f'<td class=dim>{st.get("pct", "—")}%</td>'
            f'<td class=dim>{st.get("min", "—")} – {st.get("max", "—")}</td>'
            f'<td class=dim>{st.get("mean", "—")}</td></tr>')
    if not rows:
        return ""
    return (
        f'<div class=card><h3>放到市场里看（动态市盈率 {forward_pe}）</h3>'
        f'<table><tr><th>基准</th><th>当前动态PE</th>'
        f'<th>本股相对基准</th><th>基准所处分位</th>'
        f'<th>基准区间</th><th>基准均值</th></tr>{"".join(rows)}</table>'
        f'<div class=dim style="margin-top:8px;font-size:.82em">'
        f'"基准所处分位"= 基准自己的动态市盈率在 2011 年以来的位置，'
        f'越高说明整个市场/板块越贵。这两列要一起看：本股相对基准便宜、'
        f'但基准本身在高位，那只是"贵里面比较不贵"。<br>'
        f'2011 年起算是因为 2002~2010 年板块总盈利多次逼近零，'
        f'除出来的市盈率会飙到一两百倍，那是分母趋近于零的除法爆炸，'
        f'不是真实成交的估值。</div>'
        f'<div class=dim style="margin-top:4px;font-size:.75em">'
        f'动态市盈率历史来源：historyofmarket.com（CC BY 4.0）· 缓存 6 小时'
        f'</div></div>')


def _dossier_benchmark_chart_html(ticker: str, bench: dict) -> str:
    """指数/板块自己的历史动态市盈率曲线——就是彭博 BEst P/E 1BF 那条线。"""
    if not bench or not bench.get("series"):
        return ""
    from . import dossier as _dossier
    import json as _json
    s = bench["series"]
    st = _dossier.benchmark_percentile(s)
    cid = "bench_" + "".join(c for c in ticker if c.isalnum())[:16]
    data_json = escape(_json.dumps({
        "labels": [p["date"] for p in s],
        "fwd": [p["value"] for p in s],
        "trailing": bench.get("trailing") or [],
        "mean": st.get("mean"),
    }, ensure_ascii=False))
    return f"""<div style="margin-top:14px">
<div class=dim style="font-size:.85em;margin-bottom:6px">
历史动态市盈率（12 个月一致预期）——{len(s)} 个数据点，
{s[0]['date']} 至 {s[-1]['date']}。当前 {bench['current']}，
2011 年以来区间 {st.get('min')}–{st.get('max')}，均值 {st.get('mean')}，
当前处在 <b>{st.get('pct')}% 分位</b>。
这正是彭博终端里 BEst P/E Ratio 1BF 那条线，雅虎给不了。</div>
<canvas id="{cid}" height="80" data-bench="{data_json}"></canvas>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
(function(){{
  var el = document.getElementById("{cid}");
  if (!el || typeof Chart === "undefined") return;
  var d = JSON.parse(el.dataset.bench);
  var ds = [{{label: "动态市盈率", data: d.fwd, borderColor: "#e0a458",
             pointRadius: 0, borderWidth: 1.6, tension: 0.1}}];
  if (d.mean) {{
    ds.push({{label: "2011年来均值 " + d.mean,
      data: d.labels.map(function(){{ return d.mean; }}),
      borderColor: "#6b6b73", borderWidth: 1, borderDash: [5, 4],
      pointRadius: 0, tension: 0}});
  }}
  new Chart(el, {{
    type: "line", data: {{labels: d.labels, datasets: ds}},
    options: {{responsive: true,
      interaction: {{mode: "nearest", intersect: false}},
      plugins: {{tooltip: {{callbacks: {{label: function(ctx){{
        if (ctx.dataset.label === "动态市盈率") return "动态市盈率 " + ctx.raw;
        return ctx.dataset.label;
      }}}}}}}},
      scales: {{x: {{ticks: {{maxTicksLimit: 9}}}}}}}}
  }});
}})();
</script></div>"""


def _dossier_pe_chart_html(ticker: str, pe_hist: list[dict],
                           forward_pe: float | None) -> str:
    """静态市盈率历史折线 + 当前动态市盈率的水平参考线。

    只有一条历史线（静态 PE），因为历史的"动态市盈率"需要每个历史时点
    上的分析师一致预期，那是彭博/FactSet 的付费数据，免费源拿不到；
    动态市盈率只有"当前"这一个值，所以画成一条横的参考线。"""
    if not pe_hist:
        return ""
    import json as _json
    cid = "pechart_" + "".join(c for c in ticker if c.isalnum())[:20]
    pes = [p["pe"] for p in pe_hist]
    avg = round(sum(pes) / len(pes), 2)
    data_json = escape(_json.dumps({
        "labels": [p["date"] for p in pe_hist],
        "pe": pes, "avg": avg, "forward": forward_pe,
    }, ensure_ascii=False))
    fwd_note = (f'，虚线是当前动态市盈率 {forward_pe}' if forward_pe else "")
    return f"""<div style="margin-top:14px">
<div class=dim style="font-size:.85em;margin-bottom:6px">静态市盈率走势（近 5 年）——
点线是 {len(pe_hist)} 个交易日的静态市盈率，横线是这段期间的均值 {avg}{fwd_note}。
EPS 按最近一个已公布财年计，所以线在财年交界处会有台阶；亏损年份没有市盈率，线会断开。</div>
<canvas id="{cid}" height="70" data-pe="{data_json}"></canvas>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
(function(){{
  var el = document.getElementById("{cid}");
  if (!el || typeof Chart === "undefined") return;
  var d = JSON.parse(el.dataset.pe);
  var ds = [
    {{label: "静态市盈率", data: d.pe, borderColor: "#9ece6a",
      pointRadius: 0, borderWidth: 1.6, tension: 0.15}},
    {{label: "期间均值 " + d.avg, data: d.labels.map(function(){{ return d.avg; }}),
      borderColor: "#6b6b73", borderWidth: 1, pointRadius: 0, tension: 0}}
  ];
  if (d.forward !== null && d.forward !== undefined) {{
    ds.push({{label: "当前动态市盈率 " + d.forward,
      data: d.labels.map(function(){{ return d.forward; }}),
      borderColor: "#e0a458", borderDash: [6, 4], borderWidth: 1.5,
      pointRadius: 0, tension: 0}});
  }}
  new Chart(el, {{
    type: "line", data: {{labels: d.labels, datasets: ds}},
    options: {{
      responsive: true,
      interaction: {{mode: "nearest", intersect: false}},
      plugins: {{tooltip: {{callbacks: {{label: function(ctx){{
        if (ctx.dataset.label === "静态市盈率") return "静态市盈率 " + ctx.raw;
        return ctx.dataset.label;
      }}}}}}}},
      scales: {{x: {{ticks: {{maxTicksLimit: 8}}}}}}
    }}
  }});
}})();
</script></div>"""


def _dossier_valuation_html(ticker: str | None, val: dict | None,
                            current_price: float | None,
                            pe_hist: list[dict] | None = None,
                            own_bench: dict | None = None) -> str:
    """估值小卡片：动态市盈率（远期 PE）+ 静态市盈率（TTM）+ 一致预期 EPS。

    数据全部来自雅虎财经（yfinance），不需要额外 API key、不花钱。覆盖上
    的真实情况：普通个股（含台股/韩股）基本都有远期 PE；ETF 通常只有
    静态 PE，因为 ETF 没有"一致预期 EPS"这个概念；指数两个都没有；成交
    太清淡的小票也可能查不到。查不到的就明说查不到，不假装有数据。"""
    if not ticker:
        return ""
    val = val or {}
    own_bench = own_bench or {}
    fwd, ttm = val.get("forward_pe"), val.get("trailing_pe")
    # 指数/板块本身：雅虎给不了它的市盈率，但我们有真正的历史动态市盈率
    # 序列（historyofmarket），直接用那条线，比雅虎的空值有用得多。
    if own_bench and own_bench.get("current") is not None:
        chart = _dossier_benchmark_chart_html(ticker, own_bench)
        return (f'<div class=card><h3>估值（{escape(ticker)} · '
                f'{escape(own_bench["label"])}）</h3>'
                f'<div style="display:flex;gap:26px;flex-wrap:wrap">'
                f'{_val_cell("动态市盈率 (Forward P/E)", own_bench["current"], "12 个月一致预期")}'
                f'{_val_cell("当前价", current_price, "最新收盘") if current_price is not None else ""}'
                f'</div>{chart}'
                f'<div class=dim style="margin-top:4px;font-size:.75em">'
                f'来源：historyofmarket.com（CC BY 4.0）· 缓存 6 小时</div></div>')
    if fwd is None and ttm is None:
        return ('<div class=card class=dim style="font-size:.9em">'
                f'估值：雅虎财经没有 {escape(ticker)} 的市盈率数据'
                '（ETF、指数和部分冷门标的没有一致预期 EPS，属正常情况）。'
                '</div>')

    _cell = _val_cell
    cells = [
        _cell("动态市盈率 (Forward P/E)", fwd, "按未来 12 个月一致预期 EPS"),
        _cell("静态市盈率 (TTM P/E)", ttm, "按过去 12 个月已实现 EPS"),
    ]
    if val.get("forward_eps") is not None:
        cells.append(_cell("预期 EPS", val["forward_eps"], "未来 12 个月一致预期"))
    if current_price is not None:
        cells.append(_cell("当前价", current_price, "最新收盘"))
    cheap_note = ""
    if fwd is not None and ttm is not None:
        if fwd < ttm:
            cheap_note = ('<span class=dim style="font-size:.85em">'
                         '动态低于静态 = 市场预期盈利还在增长</span>')
        elif fwd > ttm:
            cheap_note = ('<span class=dim style="font-size:.85em">'
                         '动态高于静态 = 市场预期盈利会下滑</span>')
    pe_chart = _dossier_pe_chart_html(ticker, pe_hist or [], fwd)
    no_line_note = ""
    if not pe_chart:
        no_line_note = ('<div class=dim style="margin-top:10px;font-size:.8em">'
                       '（画不出市盈率走势线：免费源只有 4~5 个年度 EPS，'
                       '且这只标的的 EPS 口径和股价对不上——最常见的是 ADR '
                       '用美元报价、财报 EPS 用当地货币——与其画一条错的线，'
                       '不如不画。）</div>')
    return (f'<div class=card><h3>估值（{escape(ticker)}）</h3>'
            f'<div style="display:flex;gap:26px;flex-wrap:wrap;align-items:flex-start">'
            f'{"".join(cells)}</div>'
            f'<div style="margin-top:8px">{cheap_note}</div>'
            f'{pe_chart}{no_line_note}'
            f'<div class=dim style="margin-top:4px;font-size:.75em">'
            f'来源：雅虎财经 · 缓存 1 小时</div></div>')


def _dossier_summary_html(name: str, summary_row, pinned: bool) -> str:
    """AI 近期总结卡片：置顶公司有新内容会自动刷新（见 dossier.
    process_video_companies），非置顶公司只能靠这里的手动刷新按钮生成/
    更新。不管哪种，生成时间都写在总结旁边，方便判断是不是最新的。"""
    refresh_form = (
        f'<form method=post action="/companies/{escape(name)}/summary/refresh" '
        f'style="display:inline">'
        f'<input type=hidden name=_csrf value="{CSRF}">'
        f'<button style="padding:2px 10px">'
        f'{"🔄 立即刷新" if summary_row else "✨ 生成 AI 近期总结"}</button></form>')
    pin_note = ('<span class=dim style="font-size:.85em">'
               '（置顶公司，有新内容会自动刷新）</span>' if pinned else
               '<span class=dim style="font-size:.85em">（非置顶，需手动刷新）</span>')
    if summary_row is None:
        return (f'<div class=card><h3>AI 近期总结</h3>'
               f'<div class=dim>还没有生成过，最近三个月内（或最新 10 篇，哪个更少'
               f'算哪个）有相关内容时可以点这里生成。</div>'
               f'<div style="margin-top:6px">{refresh_form} {pin_note}</div></div>')
    generated = (summary_row["generated_at"] or "").replace("T", " ").rstrip("Z")
    return (f'<div class=card><h3>AI 近期总结</h3>'
           f"<pre style='white-space:pre-wrap;font-family:inherit;font-size:15.5px;"
           f"line-height:1.75;margin:0'>{escape(summary_row['summary'])}</pre>"
           f'<div class=dim style="margin-top:8px;font-size:.85em">'
           f'生成于 {escape(generated)} UTC · 覆盖 {summary_row["item_count"]} 条内容'
           f'</div>'
           f'<div style="margin-top:6px">{refresh_form} {pin_note}</div></div>')


@app.route("/companies/<name>/summary/refresh", methods=["POST"])
def company_summary_refresh(name: str):
    check_csrf()
    cfg = cfg_mod.load()
    from . import dossier as _dossier
    con = dbm.connect()
    try:
        _dossier.generate_dossier_summary(cfg, con, name)
    except Exception:
        pass
    con.close()
    return redirect(url_for("company_view", name=name))


@app.route("/companies/<name>/price-level/delete", methods=["POST"])
def company_price_level_delete(name: str):
    check_csrf()
    try:
        level_id = int(request.form.get("level_id", "0"))
    except ValueError:
        level_id = 0
    if level_id:
        con = dbm.connect()
        dbm.dossier_delete_price_level(con, level_id, name)
        con.close()
    return redirect(url_for("company_view", name=name))


def _dossier_chart_html(name: str, ticker: str | None, history: list[dict],
                        levels) -> str:
    """点位图：折线=历史收盘价(雅虎财经)，散点=视频里提到的推荐点位（按
    提及日期落在对应位置）。查不到股票代码/取不到历史价格就不渲染图表，
    只留下面的点位表格。"""
    import json as _json
    from . import dossier as _dossier
    if not ticker:
        return ('<div class=card class=dim>没能识别出对应的股票代码，'
                '不展示价格走势图（下面的点位表格仍然可用）。</div>')
    if not history:
        return (f'<div class=card class=dim>识别为 {escape(ticker)}，但暂时'
                f'取不到历史价格（网络问题或代码有误），点位表格仍然可用。</div>')

    dated = [r for r in levels if r["price"] is not None and r["mentioned_date"]]
    # 同一天里价格相近的点位先聚类合并——簇长度 1 的（没有可合并的搭档）
    # 照旧按频道分组、走原来的短横线标记；簇长度 >1 的合并成一个"合并
    # 点位"，用另一种醒目的标记单独画，鼠标移上去能看到簇里每一条的
    # 频道/类型/价格/原文。
    clusters = _dossier.cluster_nearby_price_levels(dated)
    channels_order: list[str] = []
    by_channel: dict[str, list] = {}
    merged_points: list[dict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            r = cluster[0]
            chan = r["channel"] or "未知频道"
            if chan not in by_channel:
                by_channel[chan] = []
                channels_order.append(chan)
            by_channel[chan].append(r)
        else:
            avg_price = round(sum(r["price"] for r in cluster) / len(cluster), 4)
            merged_points.append({
                "x": cluster[0]["mentioned_date"], "y": avg_price,
                "items": [{"channel": r["channel"] or "未知频道",
                          "type": LEVEL_TYPE_LABELS_ZH.get(r["level_type"], r["level_type"] or ""),
                          "price": r["price"], "text": r["raw_text"],
                          "id": r["id"]} for r in cluster],
            })
    # 按频道分组、固定调色板按"第一次出现顺序"分配颜色——同一个频道在
    # 不同公司页面上颜色也是一致的
    palette = ["#d16060", "#7aa2f7", "#e0a458", "#9ece6a", "#bb9af7",
              "#73daca", "#f7768e", "#ff9e64", "#c0caf5", "#41a6b5"]
    channel_groups = [{
        "channel": chan,
        "color": palette[i % len(palette)],
        "points": [{"x": r["mentioned_date"], "y": r["price"],
                   "type": LEVEL_TYPE_LABELS_ZH.get(r["level_type"], r["level_type"] or ""),
                   "text": r["raw_text"], "id": r["id"]} for r in by_channel[chan]],
    } for i, chan in enumerate(channels_order)]

    cid = "chart_" + "".join(c if c.isalnum() else "" for c in name)[:24] or "chart"
    current_price = history[-1]["close"] if history else None
    data_json = escape(_json.dumps({
        "labels": [h["date"] for h in history],
        "close": [h["close"] for h in history],
        "groups": channel_groups,
        "merged": merged_points,
        "current": current_price,
    }, ensure_ascii=False))
    range_btns = "".join(
        f'<button type=button class="{cls}" data-range-for="{cid}" '
        f'data-range-days="{days}" style="padding:2px 10px">{label}</button>'
        for days, label, cls in [
            ("30", "近1月", ""), ("90", "近3月", ""), ("180", "近6月", ""),
            ("365", "近1年", ""), ("1825", "近5年", ""), ("all", "全部", "primary")])
    return f"""<div class=card>
<h3>价格走势与推荐点位（{escape(ticker)}）——短横线是视频里提到的点位，
按频道上色；同一天价格相近（2% 以内）的点位合并成金色菱形显示，鼠标
移上去看是谁说的、说了什么；虚线是当前价；点击点位可以直接删掉</h3>
<div style="display:flex;gap:6px;margin-bottom:8px">{range_btns}</div>
<canvas id="{cid}" height="90" data-chart="{data_json}"></canvas>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
(function(){{
  var el = document.getElementById("{cid}");
  var d = JSON.parse(el.dataset.chart);
  var CSRF_TOK = {_json.dumps(CSRF)};
  var DELETE_URL = {_json.dumps(f"/companies/{name}/price-level/delete")};

  function idxOfLabels(labels){{
    var m = {{}};
    labels.forEach(function(l, i){{ m[l] = i; }});
    return m;
  }}
  function shiftDate(ymd, days){{
    var p = ymd.split("-").map(Number);
    var dt = new Date(Date.UTC(p[0], p[1]-1, p[2]));
    dt.setUTCDate(dt.getUTCDate() + days);
    return dt.toISOString().slice(0, 10);
  }}
  function buildDatasets(labels, idx2){{
    var origIdx = idxOfLabels(d.labels);
    var close = labels.map(function(l){{ return d.close[origIdx[l]]; }});
    var datasets = [
      {{label: "收盘价", data: close, borderColor: "#7aa2f7",
        pointRadius: 0, borderWidth: 1.5, tension: 0.15}}
    ];
    if (d.current !== null && d.current !== undefined) {{
      datasets.push({{
        label: "当前价 " + d.current, data: labels.map(function(){{ return d.current; }}),
        borderColor: "#e9e9ec", borderDash: [6, 4], borderWidth: 1.5,
        pointRadius: 0, tension: 0
      }});
    }}
    d.groups.forEach(function(g){{
      var pts = g.points.filter(function(p){{ return idx2[p.x] !== undefined; }})
        .map(function(p){{
          return {{x: p.x, y: p.y, type: p.type, text: p.text, channel: g.channel, id: p.id}};
        }});
      datasets.push({{
        label: g.channel, data: pts, type: "scatter",
        pointStyle: "dash", rotation: 0,
        backgroundColor: g.color, borderColor: g.color,
        pointRadius: 7, pointHoverRadius: 9, borderWidth: 2
      }});
    }});
    var mpts = (d.merged || []).filter(function(p){{ return idx2[p.x] !== undefined; }})
      .map(function(p){{ return {{x: p.x, y: p.y, items: p.items}}; }});
    if (mpts.length) {{
      datasets.push({{
        label: "合并点位", data: mpts, type: "scatter",
        pointStyle: "rectRot", rotation: 0,
        backgroundColor: "#e0a458", borderColor: "#1b1b1f",
        pointRadius: 8, pointHoverRadius: 11, borderWidth: 2
      }});
    }}
    return datasets;
  }}

  function closePointPopup(){{
    var old = document.getElementById("chartPointPopup_{cid}");
    if (old) old.remove();
  }}

  function deletePricePoint(levelId, btn){{
    if (!levelId) return;
    btn.disabled = true;
    btn.textContent = "…";
    var body = new URLSearchParams();
    body.set("_csrf", CSRF_TOK);
    body.set("level_id", levelId);
    fetch(DELETE_URL, {{
      method: "POST",
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: body.toString()
    }}).then(function(){{ location.reload(); }})
      .catch(function(){{ btn.disabled = false; btn.textContent = "✕"; }});
  }}

  function showPointPopup(nativeEvt, point){{
    closePointPopup();
    var items = point.items || [{{
      channel: point.channel, type: point.type, price: point.y,
      text: point.text, id: point.id
    }}];
    var box = document.createElement("div");
    box.id = "chartPointPopup_{cid}";
    box.style.cssText = "position:fixed;z-index:9999;background:#1b1b1f;" +
      "border:1px solid #333;border-radius:10px;padding:10px 14px;max-width:320px;" +
      "box-shadow:0 8px 24px rgba(0,0,0,.45);font-size:13.5px;line-height:1.6;color:#e9e9ec";
    var left = nativeEvt.clientX + 12, top = nativeEvt.clientY + 12;
    if (left + 320 > window.innerWidth) left = window.innerWidth - 330;
    box.style.left = Math.max(8, left) + "px";
    box.style.top = top + "px";
    items.forEach(function(it, i){{
      var row = document.createElement("div");
      row.style.cssText = "text-align:center" +
        (i > 0 ? ";margin-top:10px;padding-top:10px;border-top:1px solid #333" : "");
      var info = document.createElement("div");
      info.style.cssText = "white-space:pre-wrap;margin-bottom:6px";
      info.textContent = (it.channel || "") + "：" + (it.type || "点位") + " " + it.price
        + (it.text ? ("\\n" + it.text) : "");
      var delBtn = document.createElement("button");
      delBtn.textContent = "✕";
      delBtn.title = "删除这个点位";
      delBtn.style.cssText = "padding:3px 12px;border-radius:6px;cursor:pointer;" +
        "background:#3a2020;border:1px solid #7a3a3a;color:#ffb4b4;font-weight:bold";
      delBtn.addEventListener("click", function(e){{
        e.stopPropagation();
        deletePricePoint(it.id, delBtn);
      }});
      row.appendChild(info);
      row.appendChild(delBtn);
      box.appendChild(row);
    }});
    document.body.appendChild(box);
  }}

  var chart = new Chart(el, {{
    type: "line",
    data: {{labels: d.labels, datasets: buildDatasets(d.labels, idxOfLabels(d.labels))}},
    options: {{
      responsive: true,
      interaction: {{mode: "nearest", intersect: false}},
      onClick: function(evt, elements){{
        if (!elements.length) {{ closePointPopup(); return; }}
        var ei = elements[0];
        var ds = chart.data.datasets[ei.datasetIndex];
        var pt = ds.data[ei.index];
        if (!pt || (!pt.items && pt.id === undefined)) {{ closePointPopup(); return; }}
        showPointPopup(evt.native || evt, pt);
      }},
      plugins: {{tooltip: {{callbacks: {{label: function(ctx){{
        var p = ctx.raw;
        if (p && p.items) {{
          var lines = [];
          p.items.forEach(function(it){{
            lines.push(it.channel + "：" + (it.type || "点位") + " " + it.price);
            if (it.text) lines.push(it.text);
          }});
          return lines;
        }}
        if (p && p.channel) {{
          return [ctx.dataset.label + "：" + (p.type || "点位") + " " + p.y,
                  p.text || ""];
        }}
        if (ctx.dataset.label === "收盘价") return "收盘 " + ctx.raw;
        return ctx.dataset.label;
      }}}}}}}},
      scales: {{x: {{ticks: {{maxTicksLimit: 10}}}}}}
    }}
  }});

  document.addEventListener("click", function(e){{
    var popup = document.getElementById("chartPointPopup_{cid}");
    if (!popup) return;
    if (popup.contains(e.target)) return;
    if (e.target === el || el.contains(e.target)) return;
    closePointPopup();
  }});

  function applyRange(days){{
    var cutoff = null;
    if (days !== null && d.labels.length) {{
      cutoff = shiftDate(d.labels[d.labels.length - 1], -days);
    }}
    var labels = cutoff === null ? d.labels
      : d.labels.filter(function(l){{ return l >= cutoff; }});
    var idx2 = idxOfLabels(labels);
    chart.data.labels = labels;
    chart.data.datasets = buildDatasets(labels, idx2);
    chart.update();
    var tbl = document.getElementById("priceLevelTable");
    if (tbl) {{
      tbl.querySelectorAll("tbody tr[data-date]").forEach(function(tr){{
        var dt = tr.getAttribute("data-date");
        tr.style.display = (!dt || cutoff === null || dt >= cutoff) ? "" : "none";
      }});
    }}
  }}

  document.querySelectorAll('[data-range-for="{cid}"]').forEach(function(btn){{
    btn.addEventListener("click", function(){{
      var v = btn.getAttribute("data-range-days");
      applyRange(v === "all" ? null : parseInt(v, 10));
      document.querySelectorAll('[data-range-for="{cid}"]').forEach(function(b){{
        b.classList.remove("primary");
      }});
      btn.classList.add("primary");
    }});
  }});
}})();
</script>
</div>"""


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = cfg_mod.load()
    msg = ""
    upd = request.args.get("upd", "")
    if upd == "latest":
        msg = '<span class=ok>已是最新版本</span>'
    elif upd.startswith("pulling"):
        msg = ('<span class="st run">发现新版本 ' + escape(upd[7:])
               + '，正在后台更新并重启——约半分钟后重新打开窗口即为新版</span>')
    elif upd == "err":
        msg = '<span class=bad>检查更新失败（网络或 git 仓库问题）</span>'
    mres = request.args.get("models", "")
    if mres.startswith("err:"):
        msg = f'<span class=bad>拉取模型列表部分失败：{escape(mres[4:])}</span>'
    elif mres:
        msg = f'<span class=ok>模型列表已刷新（共 {escape(mres)} 个）</span>'
    if request.args.get("firstrun"):
        msg = ('<span class=bad>欢迎使用！请先在下方"API 凭证"中添加你自己的 '
               'AI 密钥（OpenAI 或 Anthropic 任一），添加后全部功能可用。</span>')
    if request.method == "POST":
        check_csrf()
        f = request.form
        if f.get("form") == "ai":
            for grp in ("article", "visuals", "qa"):
                v = f.get(f"ai_{grp}")
                if v in ("auto", "openai", "anthropic", "claude_cli", "ollama"):
                    cfg.data.setdefault("ai", {})[grp] = v
            for prov in ("openai", "anthropic", "claude_cli", "ollama"):
                mv = f.get(f"model_{prov}", "").strip()
                if mv and len(mv) < 80:
                    cfg.data.setdefault("article", {})[f"model_{prov}"] = mv
            try:
                cfg_mod.save(cfg)
                msg = '<span class=ok>AI 分工已保存</span>'
            except cfg_mod.ConfigError as e:
                msg = f'<span class=bad>{escape(str(e))}</span>'
            cfg = cfg_mod.load()
        elif f.get("form") == "keys":
            for prov in ("openai", "anthropic"):
                val = f.get(f"key_{prov}", "").strip()
                if val:
                    subprocess.run(["security", "delete-generic-password",
                                    "-s", f"ytrec-{prov}"], capture_output=True)
                    r = subprocess.run(
                        ["security", "add-generic-password", "-s", f"ytrec-{prov}",
                         "-a", "ytrec", "-w", val], capture_output=True, text=True)
                    msg += (f'<span class=ok>{prov} key 已存入钥匙串</span> '
                            if r.returncode == 0 else
                            f'<span class=bad>{prov} 保存失败</span> ')
        elif f.get("form") == "downloads":
            from . import quickdl
            dest = f.get("dest_dir", "").strip()
            q = f.get("default_quality", "1080p")
            if dest:
                cfg.data.setdefault("downloads", {})["dest_dir"] = dest
            if q in quickdl.QUALITY_FORMATS:
                cfg.data.setdefault("downloads", {})["default_quality"] = q
            try:
                cfg_mod.save(cfg)
                msg = '<span class=ok>下载设置已保存</span>'
            except cfg_mod.ConfigError as e:
                msg = f'<span class=bad>{escape(str(e))}</span>'
            cfg = cfg_mod.load()
        else:
            hours = sorted(int(h) for h in f.getlist("hour"))
            cfg.data.setdefault("scheduler", {})["hours"] = hours
            cfg.data["scheduler"]["confirm_dialog"] = f.get("confirm_dialog", "on_new_videos")
            cfg.data["discovery"]["review_gate"] = f.get("run_mode") == "confirm"
            if f.get("language") in ("zh", "en"):
                cfg.data.setdefault("app", {})["language"] = f["language"]
            cfg.data["scheduler"]["confirm_timeout_sec"] = int(f.get("timeout", 30))
            cfg.data["transcription"]["primary"] = f.get("primary", "macwhisper_watch_srt")
            cfg.data["article"]["mode"] = f.get("article_mode", "edited_article")
            cfg.data["article"]["custom_prompt"] = f.get("custom_prompt", "").strip()
            cfg.data["article"]["append_original"] = f.get("append_original") == "1"
            if f.get("punctuation") in ("ai", "basic"):
                cfg.data["article"]["punctuation"] = f["punctuation"]
            try:
                vp = int(f.get("verbatim_pct", 70))
                if vp in (0, 40, 50, 60, 70, 80, 90, 100):
                    cfg.data["article"]["verbatim_pct"] = vp
            except (TypeError, ValueError):
                pass
            cfg.data["visuals"]["image_density"] = int(f.get("density", 3))
            cfg.data["visuals"]["enabled"] = f.get("visuals_on") == "1"
            cfg.data.setdefault("dossier", {})["enabled"] = f.get("dossier_on") == "1"
            cfg.data["vault"]["root"] = f.get("vault_root", "").strip()
            layout = f.get("layout", "vault")
            if layout in ("vault", "folder_split", "folder_flat"):
                cfg.data["vault"]["layout"] = layout
                if layout == "folder_split":
                    cfg.data["vault"]["raw_subdir"] = "Raw"
                    cfg.data["vault"]["wiki_subdir"] = "Wiki"
                    cfg.data["vault"]["attachments_subdir"] = "Attachments"
                elif layout == "folder_flat":
                    cfg.data["vault"]["wiki_subdir"] = ""
                    cfg.data["vault"]["attachments_subdir"] = "images"
            old_sub = cfg.get("vault.wiki_subdir", "30-Wiki")
            new_sub = f.get("wiki_subdir", old_sub).strip().strip("/") or old_sub
            cfg.data["vault"]["wiki_subdir"] = new_sub
            if new_sub != old_sub and f.get("migrate_reports") == "1":
                root = cfg.vault_root
                if root:
                    from . import vault as _vt
                    con2 = _con()
                    try:
                        n = _vt.migrate_wiki(con2, root, old_sub, new_sub)
                        msg += f' <span class=ok>已迁移 {n} 篇文章到 {escape(new_sub)}/</span>'
                    except _vt.VaultError as e:
                        msg += f' <span class=bad>迁移失败: {escape(str(e))}</span>'
                    con2.close()
            def _int(name, default, lo, hi):
                try:
                    return min(max(int(f.get(name, default)), lo), hi)
                except (TypeError, ValueError):
                    return default
            cfg.data["discovery"]["max_new_videos_per_run"] = _int("max_per_run", 5, 1, 50)
            cfg.data["discovery"]["min_duration_sec"] = _int("min_dur", 90, 0, 3600)
            cfg.data["transcription"]["timeout_minutes"] = _int("timeout_min", 180, 10, 720)
            cfg.data["transcription"]["collect_wait_minutes"] = _int("collect_wait", 45, 0, 180)
            try:
                cfg_mod.save(cfg)
                msg = '<span class=ok>设置已保存</span>'
                if hours:
                    from . import scheduler
                    msg += f' <span class=dim>{escape(scheduler.install(hours))}</span>'
            except cfg_mod.ConfigError as e:
                msg = f'<span class=bad>{escape(str(e))}</span>'
            cfg = cfg_mod.load()

    hours_now = set(cfg.get("scheduler.hours", [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]))
    hour_boxes = "".join(
        f"<label><input type=checkbox name=hour value={h} "
        f"{'checked' if h in hours_now else ''}><span>{h:02d}</span></label>"
        for h in range(24))
    dsel = lambda v, cur: "selected" if v == cur else ""

    def _model_opts(prov: str, fallback: list) -> str:
        _defaults = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-5",
                     "claude_cli": "sonnet", "ollama": "llama3.1"}
        cur = cfg.get(f"article.model_{prov}", _defaults.get(prov, ""))
        models = _load_models().get(prov) or fallback
        if cur not in models:
            models = [cur] + models
        return "".join(
            f'<option {"selected" if m == cur else ""}>{escape(m)}</option>'
            for m in models)

    oai_opts = _model_opts("openai", ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"])
    ant_opts = _model_opts("anthropic",
                           ["claude-sonnet-5", "claude-haiku-4-5",
                            "claude-opus-4-8"])
    cli_opts = _model_opts("claude_cli", ["sonnet", "opus", "haiku"])
    oll_opts = _model_opts("ollama", ["llama3.1"])
    from .providers import _claude_cli_path, claude_cli_proxy_issue
    cli_ok = bool(_claude_cli_path())
    cli_warn = claude_cli_proxy_issue() if cli_ok else None
    oll_ok = _ollama_alive()
    cd = cfg.get("scheduler.confirm_dialog")
    tp = cfg.get("transcription.primary")
    am = cfg.get("article.mode")
    lay = cfg.get("vault.layout", "vault")
    body = f"""
<div class=card>{DOODLES['settings']}<h3>设置 · 按处理流程</h3>{msg}
<p class=dim>一条视频从发现到进库要经过六个环节。下面按发生顺序排列：每段先讲这一步会发生什么，紧接着就是它的可调项。</p></div>
<form method=post>
<input type=hidden name=_csrf value={CSRF}>

<div class=card><h3>⓪ 语言 / Language</h3>
<table class=wrap>
<tr><td>界面语言 / UI language</td><td><select name=language onchange="fetch('/set-language',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'_csrf={CSRF}&lang='+this.value}}).then(()=>location.reload())">
<option value=zh {dsel('zh', cfg.get('app.language','zh'))}>中文</option>
<option value=en {dsel('en', cfg.get('app.language','zh'))}>English</option>
</select></td></tr>
<tr><td>软件更新 / Updates</td><td>当前版本 v{__version__}
 <button formaction=/update formmethod=post>🔄 检查更新</button>
 <span class=dim>按 GitHub Release 版本更新（未发布的提交不会推送）</span></td></tr>
</table>
<p class=dim>开始之前 / Before you start：<br>
1. 界面语言选择后立即生效，无需保存 / UI language applies immediately when selected.<br>
2. 本软件默认不内置任何 API key，需要你自己添加（见第 ⑥ 节）/ No API key ships by default — add your own in section ⑥.<br>
3. 各 AI 环节可分别指定使用哪个 API（也在第 ⑥ 节）/ Each AI stage can use a different provider — also in section ⑥.</p></div>

<div class=card><h3>订阅 · 导入 / 导出</h3><div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
<button type=button onclick="location.href='/channels/export'">⬇ 导出订阅</button>
<form method=post action=/channels/import enctype="multipart/form-data"
 style="display:flex;gap:6px;align-items:center;margin:0">
<input type=hidden name=_csrf value={CSRF}>
<input type=file name=file accept=".json" required style="font-size:12px;max-width:240px">
<button>⬆ 导入订阅</button></form>
<span class=dim>导出含分组/启停/起始日期；导入按频道合并，不会重复添加</span>
</div></div><div class=card><h3>① 嗅探 · 发现新视频</h3>
<p class=dim>按排班表整点唤醒（睡眠错过的唤醒后合并补跑），逐个检查订阅频道的 RSS。发现新视频后：自动模式直接进入处理，确认模式先在 Queue 列出等你逐条确认——两种模式都能随时跳过单条。</p>
<div class=hours>{hour_boxes}</div>
<table class=wrap>
<tr><td>运行模式</td><td><select name=run_mode>
<option value=auto {'selected' if not cfg.get('discovery.review_gate') else ''}>自动：发现后直接处理（Queue 中可随时跳过）</option>
<option value=confirm {'selected' if cfg.get('discovery.review_gate') else ''}>确认：发现后先列出，逐条确认/跳过</option>
</select></td></tr>
<tr><td>发现新视频弹窗</td><td><select name=confirm_dialog>
<option value=on_new_videos {dsel('on_new_videos',cd)}>仅发现新视频时弹</option>
<option value=always {dsel('always',cd)}>每次运行都弹</option>
<option value=never {dsel('never',cd)}>从不弹窗</option></select>
 倒计时 <input name=timeout size=3 value={cfg.get('scheduler.confirm_timeout_sec',30)}> 秒</td></tr>
<tr><td>单轮最多处理</td><td><input name=max_per_run size=4
 value={cfg.get('discovery.max_new_videos_per_run',5)}> 条 <span class=dim>（其余留到下一轮，防首跑洪水）</span></td></tr>
</table></div>

<div class=card><h3>② 下载 · 拿到字幕或音频</h3>
<p class=dim>先探测：有现成字幕就直接抓取——零成本、几秒完成、跳过转录；无字幕才下载原生 m4a 音频。直播和过短的视频自动跳过。</p>
<table class=wrap>
<tr><td>跳过短视频</td><td>短于 <input name=min_dur size=5
 value={cfg.get('discovery.min_duration_sec',90)}> 秒的视频（Shorts）不处理</td></tr>
</table></div>

<div class=card><h3>③ 转录 · 音频变文字</h3>
<p class=dim>主路径把音频投进 MacWhisper 监视文件夹并守候出稿（本地、免费）；或直接走 OpenAI API——超 24MB 自动压缩，仍超限则切段、段间 15 秒重叠、按时间码无缝合并。MacWhisper 超时会自动切 API 兜底，不会卡死。</p>
<table class=wrap>
<tr><td>转录方式</td><td><select name=primary>
<option value=macwhisper_watch_srt {dsel('macwhisper_watch_srt',tp)}>MacWhisper 监视文件夹（本地）</option>
<option value=openai_audio {dsel('openai_audio',tp)}>OpenAI Whisper API（云端）</option>
<option value=whisper_cpp {dsel('whisper_cpp',tp)}>whisper.cpp（本地）</option></select></td></tr>
<tr><td>MacWhisper 超时</td><td><input name=timeout_min size=4
 value={cfg.get('transcription.timeout_minutes',180)}> 分钟无出稿 → 自动转 API 兜底</td></tr>
<tr><td>守候回收</td><td>投稿后 <input name=collect_wait size=4
 value={cfg.get('transcription.collect_wait_minutes',45)}> 分钟内每 20 秒检查一次，出稿立即进入下一环节</td></tr>
</table></div>

<div class=card><h3>④ 整理 · AI 成文与配图</h3>
<p class=dim>完整文稿分块做忠实笔记（并行调用），再全局组稿成文章——不编造事实、每段可溯源到时间码；随后按内容语义截取视频画面插入对应章节（叙述式视频由 AI 推断画面时刻）。</p>
<table class=wrap>
<tr><td>文章模式</td><td><select name=article_mode>
<option value=edited_article {dsel('edited_article',am)}>整理成文（重组结构）</option>
<option value=faithful_cleanup {dsel('faithful_cleanup',am)}>忠实清稿（只去口头禅）</option></select></td></tr>
<tr><td>原文保留比例</td><td><select name=verbatim_pct>
<option value=0 {dsel(0, cfg.get('article.verbatim_pct',70))}>关闭（自由整理）</option>
<option value=40 {dsel(40, cfg.get('article.verbatim_pct',70))}>40%（可重排句序）</option>
<option value=50 {dsel(50, cfg.get('article.verbatim_pct',70))}>50%（可重排句序）</option>
<option value=60 {dsel(60, cfg.get('article.verbatim_pct',70))}>60%（可重排句序）</option>
<option value=70 {dsel(70, cfg.get('article.verbatim_pct',70))}>70%（推荐）</option>
<option value=80 {dsel(80, cfg.get('article.verbatim_pct',70))}>80%</option>
<option value=90 {dsel(90, cfg.get('article.verbatim_pct',70))}>90%</option>
<option value=100 {dsel(100, cfg.get('article.verbatim_pct',70))}>100%（纯原文分节）</option>
</select>
<p class=dim>硬约束：正文中至少该比例的字符逐字来自原文（AI 只负责选句和过渡，
被选句子由程序原样拷贝，不足自动补齐；实测值写入文章 frontmatter。
70% 以下档位允许 AI 重排句子先后组合，70% 及以上严格保持原文语序）。</p></td></tr>
<tr><td>标点方式</td><td><select name=punctuation>
<option value=ai {dsel('ai', cfg.get('article.punctuation','ai'))}>AI 重标点（剥标点逐字校验，内容零改动）</option>
<option value=basic {dsel('basic', cfg.get('article.punctuation','ai'))}>机械补标点（句界补逗号/句号）</option>
</select></td></tr>
<tr><td>原文附加</td><td><label><input type=checkbox name=append_original value=1
 {'checked' if cfg.get('article.append_original', True) else ''}>
 AI 改写在前，完整原文以可折叠块附在文末（Obsidian 中默认收起）</label></td></tr>
<tr><td>整理附加 Prompt</td><td>
<textarea name=custom_prompt rows=4 style="width:100%;border-radius:8px;padding:8px"
 placeholder="例：多保留讲者的原话引用；结尾加一段『对我的投资组合的启示』；语气偏口语。">{escape(cfg.get('article.custom_prompt',''))}</textarea>
<p class=dim>会追加到 AI 整理的系统提示词末尾；忠实性规则（不编造事实）始终优先。</p></td></tr>
<tr><td>自动截图</td><td><label><input type=checkbox name=visuals_on value=1
 {'checked' if cfg.get('visuals.enabled') else ''}> 启用</label>
 &nbsp;图片密度 <input type=range name=density min=1 max=5
 value={cfg.get('visuals.image_density',3)}
 oninput="this.nextElementSibling.textContent=this.value">
 <b>{cfg.get('visuals.image_density',3)}</b>/5
 <p class=dim>1=只截关键画面 … 5=最密：程序保证每个自然段至少一张配图（无命中画面时按该段时间点自动截取）。</p></td></tr>
</table></div>

<div class=card><h3>⑤ 阅读与保存 · 写入你的库</h3>
<p class=dim>成品原子写入下方位置并读回校验；之后在 Reports 页阅读、按时间轴浏览、按标签筛选，也可以对任何一篇"问 AI"或让它重新总结。删除的文章进回收站，3 天内可恢复。</p>
<table class=wrap>
<tr><td>保存模式</td><td><select name=layout>
<option value=vault {dsel('vault', lay)}>Obsidian Vault（20-Raw/30-Wiki 分层治理）</option>
<option value=folder_split {dsel('folder_split', lay)}>单独文件夹 · Raw+Wiki 分层</option>
<option value=folder_flat {dsel('folder_flat', lay)}>单独文件夹 · 纯平铺（原文折叠随文章，无 Raw 副本）</option>
</select></td></tr>
<tr><td>保存根目录</td><td><input name=vault_root style="width:100%"
 value="{escape(cfg.get('vault.root',''))}"
 placeholder="Obsidian 库根目录，或任意目标文件夹"></td></tr>
<tr><td>Reports 保存位置</td><td>
<input name=wiki_subdir style="width:60%" value="{escape(cfg.get('vault.wiki_subdir','30-Wiki'))}"
 placeholder="相对根目录，如 30-Wiki">
&nbsp;<label><input type=checkbox name=migrate_reports value=1 checked>
 修改时把现有文章迁移过去</label></td></tr>
<tr><td>公司档案插件</td><td><label><input type=checkbox name=dossier_on value=1
 {'checked' if cfg.get('dossier.enabled') else ''}> 启用</label>
 <p class=dim>默认关闭。开启后每篇新文章写入库里时，会顺带把它提到的公司/实体
 增量抽取「观点评价」「关注点」「推荐点位」，写进 50-公司档案/&lt;公司名&gt;.md
 （按公司名建档，新信息持续追加进同一篇笔记，不会新建或覆盖），每条都标注来源
 文章。开启后导航栏会出现"公司档案"入口。</p></td></tr>
</table>
<p><button class=primary>保存全部设置</button></p></div>
</form>

<div class=card id=downloads><h3>下载设置</h3>
<p class=dim>粘贴链接下载视频（<a href=/download>下载页</a>）用到的保存位置与默认清晰度，在此配置。</p>
<form method=post style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input type=hidden name=_csrf value={CSRF}><input type=hidden name=form value=downloads>
<span class=dim>保存到</span>
<input name=dest_dir value="{escape(str(cfg.get('downloads.dest_dir', '')))}" style="flex:1;min-width:260px">
<span class=dim>默认清晰度</span>
<select name=default_quality>{"".join(
    f'<option value={q} {"selected" if q == cfg.get("downloads.default_quality", "1080p") else ""}>{label}</option>'
    for q, label in [("best", "最高画质（体积最大）"), ("2160p", "4K（2160p）"),
                     ("1080p", "1080p"), ("720p", "720p"), ("480p", "480p"),
                     ("audio", "仅音频")])}</select>
<button>保存</button>
</form></div>

<div class=card><h3>⑥ AI / API</h3><p class=dim>AI 凭证与分工、Qwen/Kimi、语音识别接口已移到独立的 <a href=/api>API 页</a>，让设置更清爽。</p></div>"""
    return page("设置", "settings", body)


def main(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    if open_browser:
        import threading, webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    print(f"{BRANDING} GUI → http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
