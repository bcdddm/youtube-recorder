"""YouTube Recorder GUI · By Leoluchino  (P9, v0.2 §7)

Local-only Flask app: Channels / Queue / Reports / Settings(排班表).
Security: binds 127.0.0.1 only; per-session CSRF token on every POST;
vault file routes are canonical-path-checked against the vault root;
API keys go straight to the macOS Keychain via `security`, never to disk.
"""

from __future__ import annotations

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

CSRF = secrets.token_hex(16)
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
 body{background:var(--bg);color:var(--fg);font:14.5px/1.65 -apple-system,"PingFang SC",sans-serif;margin:0}
 ::selection{background:var(--accsel)}
 nav{display:flex;gap:4px;padding:10px 22px;background:var(--navbg);
     backdrop-filter:blur(12px);position:sticky;top:0;z-index:9;
     border-bottom:1px solid var(--line);align-items:center}
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
   word-break:break-all}
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
 {% for p,l in [('channels','Channels'),('queue','Queue'),('reports','Reports'),('settings','Settings')] %}
 <a href="/{{p}}" class="{{'on' if page==p else ''}}">{{l}}</a>{% endfor %}
 <button id=themebtn style="margin-left:auto;border:none;background:transparent;color:var(--dim);padding:4px 8px;display:flex;align-items:center"></button>
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
</body></html>"""


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
    ("保存模式", "Storage mode"), ("保存根目录", "Root folder"),
    ("Reports 保存位置", "Reports folder"),
    ("保存全部设置", "Save all settings"), ("保存 key", "Save keys"),
    ("已配置", "configured"), ("未配置", "not set"),
    ("处理进度", "Progress"), ("频道", "Channel"), ("标题", "Title"),
    ("时长", "Length"), ("发布", "Published"), ("状态", "Status"),
    ("详情", "Detail"), ("更新于", "Updated"),
    ("立即运行（强制刷新数据）", "Run now (force refresh)"),
    ("粘贴单个视频链接，直接处理…", "Paste a video link to process it…"),
    ("＋添加", "＋Add"), ("重试", "Retry"), ("跳过", "Skip"),
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
]


def _tr(html: str) -> str:
    if cfg_mod.load().get("app.language", "zh") != "en":
        return html
    for zh, en in EN_MAP:
        html = html.replace(zh, en)
    return html


def _keys_ok() -> bool:
    return _key_status("openai") or _key_status("anthropic")


def page(title: str, page_id: str, body: str):
    if page_id != "settings" and not _keys_ok():
        body = ('<div class=banner>⚠️ 尚未配置 AI 密钥——文章生成与智能截图不可用。'
                '<a href="/settings">前往设置添加 →</a></div>') + body
    return _tr(render_template_string(BASE, title=title, page=page_id,
                                      body=body, version=__version__))


def check_csrf():
    if request.form.get("_csrf") != CSRF:
        abort(403)


def _con():
    return dbm.connect()


# --- Channels -------------------------------------------------------------

@app.route("/")
def index():
    if not _keys_ok():
        return redirect(url_for("settings", firstrun=1))
    return redirect(url_for("channels"))


@app.route("/channels", methods=["GET", "POST"])
def channels():
    con = _con()
    msg = ""
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
        elif f.get("bulk") == "setgroup" and ids:
            gname = f.get("grpname", "").strip()[:20]
            con.executemany("UPDATE channels SET grp=? WHERE channel_id=?",
                            [(gname, i) for i in ids]); con.commit()
            msg = (f'<span class=ok>已把 {len(ids)} 个频道设为组'
                   f'「{escape(gname) or "（无组）"}」</span>')
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
            if url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/"):
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
                msg = '<span class=bad>请输入 youtube.com 频道链接</span>'

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
            grp = r["grp"] or ""
        except (KeyError, IndexError):
            grp = ""
        parts.append(
            "<tr><td>" + chk + "</td>"
            + "<td>" + str(escape(r["name"] or ""))
            + (("<br><span class=chip style='margin-left:0'>" + str(escape(grp))
                + "</span>") if grp else "") + "</td>"
            + "<td class=dim>" + str(cid) + "</td>"
            + "<td class=dim>" + str(escape((r["not_before"] or "")[:10])) + "</td>"
            + "<td>" + stbadge + "</td><td>" + acts + "</td></tr>")
    rows = "".join(parts)
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
<input name=url placeholder="https://www.youtube.com/@频道" style="flex:1;min-width:280px">
<input name=not_before type=date title="只处理此日期之后发布的视频（留空=从现在起）">
<button class=primary>添加</button></form>
<p class=dim>日期留空 = 从添加时刻起只收新视频，不回填历史。</p></div>
<div class=card><h3>已订阅频道</h3>
<form method=post>
<input type=hidden name=_csrf value={CSRF}>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
<label><input type=checkbox onchange="document.querySelectorAll('input[name=cid]:not(:disabled)').forEach(c=>c.checked=this.checked)"> 全选</label>
<button name=bulk value=enable>批量启用</button>
<button name=bulk value=disable>批量停用</button>
<button name=bulk value=delete
 onclick="return confirm('批量删除所选频道？已生成的文章会保留（归入手动添加）。')">批量删除</button>
<span style="margin-left:12px">组名 <input name=grpname size=8 placeholder="如 财经">
<button name=bulk value=setgroup>设为组</button></span>
</div>
<table><tr><th></th><th>名称</th><th>ID</th><th>起始日期</th><th>状态</th><th></th></tr>
{rows or '<tr><td colspan=6 class=dim>暂无</td></tr>'}</table>
</form></div>
{sugg_html}"""
    con.close()
    return page("频道", "channels", body)


# --- Queue ------------------------------------------------------------------

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
    from .paths import APP_SUPPORT, py_cmd
    subprocess.Popen(
        py_cmd() + ["-m", "youtube_recorder.cli", "run", "--once",
         "--headless"],
        cwd=str(Path(__file__).resolve().parents[1]),
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
    return {"counts": counts,
            "pending": [dict(r) for r in pending],
            "rows": [dict(r) for r in vids]}


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
            for r in con.execute("SELECT video_id FROM videos "
                                 "WHERE status='discovered' AND approved=0"):
                dbm.approve_video(con, r["video_id"])
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
            if v and v["status"] in (st.FAILED, st.DEAD_LETTER):
                dbm.set_status(con, f["retry"], f.get("stage", st.DISCOVERED))
        if f.get("approve") or f.get("approve_all"):
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
      <td class=dim>${{esc((v.published_at||'').slice(0,10))}}</td>
      <td>${{btn('approve',v.video_id,'▶ 处理','primary')}} ${{btn('skip',v.video_id,'跳过')}}</td></tr>`).join('');
    const running = ['metadata_ready','caption_check','audio_queued',
      'awaiting_transcription','transcript_ready','article_ready',
      'visual_planned','frames_ready','package_ready','written'];
    document.getElementById('rows').innerHTML = d.rows.length ? d.rows.map(v=>{{
      const cls = v.status==='verified'?'ok'
        :(v.status==='failed'||v.status==='dead_letter')?'bad'
        :(running.includes(v.status)?'run':'');
      let act='';
      if (v.status==='failed'||v.status==='dead_letter')
        act = btn('retry',v.video_id,'重试');
      const skippable = ['discovered','metadata_ready','caption_check','audio_queued',
                         'awaiting_transcription','transcript_ready','article_ready'];
      if (skippable.includes(v.status))
        act += ' ' + btn('skip',v.video_id,'跳过');
      let det = v.error_code || v.last_detail || '';
      if (v.status === 'awaiting_transcription' && v.updated_at) {{
        const mins = Math.max(0, Math.round((Date.now() - Date.parse(v.updated_at)) / 60000));
        det = `已等待 ${{mins}} 分钟（出稿后自动继续）`;
      }}
      return `<tr><td class=dim>${{esc(v.cname)}}</td>
        <td class=t title="${{esc(v.title||v.video_id)}}"><div class=clamp>${{esc(v.title||v.video_id)}}</div></td>
        <td class=dim>${{esc(_dur(v.duration_sec))}}</td>
        <td class=dim>${{esc((v.published_at||'').slice(0,10))}}</td>
        <td><span class="st ${{cls}}">${{Z[v.status]||v.status}}</span></td>
        <td class="dim t" style="max-width:220px" title="${{esc(det)}}"><div class=clamp>${{esc(det)}}</div></td>
        <td class=dim>${{esc((v.updated_at||'').slice(11,19))}}</td><td>${{act}}</td></tr>`;
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
    from .paths import APP_SUPPORT, py_cmd
    subprocess.Popen(
        py_cmd() + ["-m", "youtube_recorder.cli", "run", "--once",
         "--headless"],
        cwd=str(Path(__file__).resolve().parents[1]),
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
    out = []
    for r in rows:
        tags = []
        try:
            aj = work_dir(r["video_id"]) / "article.json"
            if aj.exists():
                tags = _json.loads(aj.read_text(encoding="utf-8")).get("tags", [])[:6]
        except Exception:
            pass
        out.append({
            "video_id": r["video_id"],
            "title": Path(r["note_path"]).stem.rsplit("--", 1)[0],
            "channel": r["cname"] or "未知频道",
            "published": (r["published_at"] or "")[:10],
            "generated": r["at"][:10],
            "duration_sec": r["duration_sec"] or 0,
            "tags": tags,
            "grp": (r["cgrp"] or "") if "cgrp" in r.keys() else "",
        })
    con.close()
    from flask import jsonify
    return jsonify(out)


DIGEST_SYSTEM = """你是情报汇总编辑。给你某一天收到的多篇视频文章的元信息
（标题/频道/摘要/要点）。输出当日汇总报告（Markdown）：
# 当日情报汇总（{date}）
先写 3-5 句总览；然后按主题归并要点（每条标注来源视频标题）；
最后一节"值得注意"：各来源间的分歧观点或共同强调的信号。
只依据给定材料，不补充外部信息。"""


@app.route("/reports/digest", methods=["POST"])
def reports_digest():
    check_csrf()
    date = request.form.get("date", "")[:10]
    grp = request.form.get("grp", "").strip()
    con = _con()
    rows = con.execute(
        "SELECT w.video_id, w.note_path, v.title, v.published_at, "
        "c.name cname, c.grp cgrp "
        "FROM writes w JOIN videos v USING(video_id) "
        "LEFT JOIN channels c USING(channel_id) "
        "WHERE w.note_kind='wiki' GROUP BY w.video_id").fetchall()
    import json as _json
    from .paths import work_dir
    items = []
    for r in rows:
        day = (r["published_at"] or r["note_path"] or "")[:10]
        if (r["published_at"] or "")[:10] != date:
            continue
        if grp and (r["cgrp"] or "") != grp:
            continue
        meta = {}
        aj = work_dir(r["video_id"]) / "article.json"
        try:
            if aj.exists():
                meta = _json.loads(aj.read_text(encoding="utf-8"))
        except Exception:
            pass
        items.append({
            "title": meta.get("title_zh") or r["title"] or r["video_id"],
            "channel": r["cname"] or "",
            "summary": meta.get("summary", ""),
            "takeaways": meta.get("takeaways", [])[:6],
        })
    con.close()
    if not items:
        body = ('<div class=card><a class=dim href="/reports">← 返回</a>'
                '<p class=dim>该日期（' + escape(date) + '）'
                + (('组「' + escape(grp) + '」') if grp else '')
                + '没有可汇总的文章。</p></div>')
        return page("日报", "reports", body)
    from . import providers
    import json as _j
    user = _j.dumps(items, ensure_ascii=False)[:40000]
    try:
        md = providers.complete(cfg_mod.load(), None, f"digest-{date}",
                                DIGEST_SYSTEM.format(date=date), user,
                                max_tokens=3000, purpose="report_qa")
    except Exception as e:
        md = f"生成失败：{e}"
    html = _md_to_html(md, "digest")
    scope = ('组「' + str(escape(grp)) + '」 · ') if grp else ""
    body = (f'<div class=card><a class=dim href="/reports">← 返回时间轴</a>'
            f'<span class=dim style="margin-left:10px">{scope}{escape(date)}'
            f' · 共 {len(items)} 篇</span>'
            f'<div class=md>{html}</div></div>')
    return page("日报", "reports", body)


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
<div class=tabs>
<button data-m=timeline class=on>📅 时间轴</button>
<button data-m=read>📖 阅读</button>
<button data-m=manage>🗂 管理</button>
</div>
<div class=card style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<input id=q placeholder="搜索标题…" style="flex:1;min-width:180px" oninput="render()">
<select id=chan onchange="render()"><option value="">全部频道</option></select>
<select id=grpsel onchange="render()"><option value="">全部组</option></select>
<select id=grp onchange="render()">
<option value=date>按日期分组</option>
<option value=channel>按频道分组</option>
<option value=flat>平铺列表</option></select>
<span id=count class=dim></span></div>
<div class=card id=tagbar style="display:none"><span class=dim style="margin-right:6px">标签：</span><span id=tags></span></div>
<div id=list></div>
<div class=card id=trashcard style="display:none"><h3>🗑 回收站 <span class=dim>· 保留 3 天后自动清除</span></h3>
<table><thead><tr><th>标题</th><th>删除于</th><th>剩余</th><th></th></tr></thead>
<tbody id=trashrows></tbody></table></div>
<div id=dd-empty style="display:none"><div class=empty>__DOODLE__这里还什么都没有</div></div>
<script>
const CSRF_T = "__CSRF__";
let DATA = [], MODE = 'timeline', TAG = null;
function esc(s) { return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]); }
function dur(s) { if(!s) return ''; const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return h? h+'小时'+m+'分' : m+'分钟'; }
function filtered() {
  const q = document.getElementById('q').value.toLowerCase();
  const ch = document.getElementById('chan').value;
  const gsel = document.getElementById('grpsel').value;
  return DATA.filter(r => (!q || r.title.toLowerCase().includes(q) || r.channel.toLowerCase().includes(q))
                       && (!ch || r.channel === ch)
                       && (!gsel || (r.grp||'') === gsel)
                       && (!TAG || (r.tags||[]).includes(TAG)));
}
function tagsHtml(r) {
  return (r.tags||[]).map(t=>`<span class=tagchip onclick="setTag('${esc(t)}');event.stopPropagation()">${esc(t)}</span>`).join('');
}
function setTag(t) { TAG = (TAG === t) ? null : t; render(); }
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
  const gsel = document.getElementById('grpsel').value;
  const f = document.createElement('form'); f.method='post'; f.action='/reports/digest';
  f.innerHTML = `<input type=hidden name=_csrf value="${CSRF_T}">
    <input type=hidden name=date value="${d}">
    <input type=hidden name=grp value="${gsel}">`;
  document.body.appendChild(f); f.submit();
}
function bulkDelete() {
  const ids = [...document.querySelectorAll('.mgc:checked')].map(c=>c.value);
  if (!ids.length) return alert('先勾选要删除的文章');
  if (!confirm(`删除所选 ${ids.length} 篇？将移入回收站，3 天内可恢复。`)) return;
  const f = document.createElement('form'); f.method='post'; f.action='/reports/bulk-delete';
  f.innerHTML = `<input type=hidden name=_csrf value="${CSRF_T}"><input type=hidden name=ids value="${ids.join(',')}">`;
  document.body.appendChild(f); f.submit();
}
function render() {
  const rows = filtered();
  document.getElementById('count').textContent = `${rows.length} 篇` + (TAG ? ` · #${TAG}` : '');
  // 标签栏
  const all = new Set(); DATA.forEach(r=>(r.tags||[]).forEach(t=>all.add(t)));
  const tb = document.getElementById('tagbar');
  tb.style.display = all.size ? '' : 'none';
  document.getElementById('tags').innerHTML = [...all].sort().map(t=>
    `<span class="tagchip ${TAG===t?'on':''}" onclick="setTag('${esc(t)}')">${esc(t)}</span>`).join('');
  const html = MODE==='timeline' ? renderTimeline(rows)
             : MODE==='manage'   ? renderManage(rows)
             : renderRead(rows);
  document.getElementById('list').innerHTML =
    html || `<div class=card>${document.getElementById('dd-empty').innerHTML.replace('这里还什么都没有','没有匹配的文章')}</div>`;
  const mg = document.getElementById('mgcount'); if (mg) mg.textContent = `共 ${rows.length} 篇`;
}
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); MODE = b.dataset.m; render();
});
(async () => {
  DATA = await (await fetch('/reports.json')).json();
  const chans = [...new Set(DATA.map(r=>r.channel))].sort();
  document.getElementById('chan').innerHTML =
    '<option value="">全部频道</option>' + chans.map(c=>`<option>${esc(c)}</option>`).join('');
  const grps = [...new Set(DATA.map(r=>r.grp).filter(Boolean))].sort();
  const gs = document.getElementById('grpsel');
  gs.style.display = grps.length ? '' : 'none';
  gs.innerHTML = '<option value="">全部组</option>' + grps.map(g=>`<option>${esc(g)}</option>`).join('');
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
    answer = request.args.get("_answer", "")
    ans_html = ""
    if answer:
        ans_html = (f"<div class=card><h3>💬 AI 回答</h3>"
                    f"<div class=md style='max-width:100%'>"
                    f"<pre style='white-space:pre-wrap'>{escape(answer)}</pre>"
                    f"</div></div>")
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
</script>""")
    return page("阅读", "reports", body)


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

def _key_status(name: str) -> bool:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s",
                            f"ytrec-{name}"], capture_output=True)
        return r.returncode == 0
    except OSError:
        return False  # non-macOS (tests)


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
    if request.args.get("firstrun"):
        msg = ('<span class=bad>欢迎使用！请先在下方"API 凭证"中添加你自己的 '
               'AI 密钥（OpenAI 或 Anthropic 任一），添加后全部功能可用。</span>')
    if request.method == "POST":
        check_csrf()
        f = request.form
        if f.get("form") == "ai":
            for grp in ("article", "visuals", "qa"):
                v = f.get(f"ai_{grp}")
                if v in ("auto", "openai", "anthropic"):
                    cfg.data.setdefault("ai", {})[grp] = v
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
            try:
                vp = int(f.get("verbatim_pct", 70))
                if vp in (0, 50, 60, 70, 80, 90, 100):
                    cfg.data["article"]["verbatim_pct"] = vp
            except (TypeError, ValueError):
                pass
            cfg.data["visuals"]["image_density"] = int(f.get("density", 3))
            cfg.data["visuals"]["enabled"] = f.get("visuals_on") == "1"
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
<tr><td>界面语言 / UI language</td><td><select name=language>
<option value=zh {dsel('zh', cfg.get('app.language','zh'))}>中文</option>
<option value=en {dsel('en', cfg.get('app.language','zh'))}>English</option>
</select></td></tr>
<tr><td>软件更新 / Updates</td><td>当前版本 v{__version__}
 <button formaction=/update formmethod=post>🔄 检查更新</button>
 <span class=dim>按 GitHub Release 版本更新（未发布的提交不会推送）</span></td></tr>
</table>
<p class=dim>开始之前 / Before you start：<br>
1. 先选择软件语言（保存后立即生效）/ Pick your UI language first — applies right after saving.<br>
2. 本软件默认不内置任何 API key，需要你自己添加（见第 ⑥ 节）/ No API key ships by default — add your own in section ⑥.<br>
3. 各 AI 环节可分别指定使用哪个 API（也在第 ⑥ 节）/ Each AI stage can use a different provider — also in section ⑥.</p></div>

<div class=card><h3>① 嗅探 · 发现新视频</h3>
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
<option value=50 {dsel(50, cfg.get('article.verbatim_pct',70))}>50%</option>
<option value=60 {dsel(60, cfg.get('article.verbatim_pct',70))}>60%</option>
<option value=70 {dsel(70, cfg.get('article.verbatim_pct',70))}>70%（推荐）</option>
<option value=80 {dsel(80, cfg.get('article.verbatim_pct',70))}>80%</option>
<option value=90 {dsel(90, cfg.get('article.verbatim_pct',70))}>90%</option>
<option value=100 {dsel(100, cfg.get('article.verbatim_pct',70))}>100%（纯原文分节）</option>
</select>
<p class=dim>硬约束：正文中至少该比例的字符逐字来自原文（AI 只负责选句和过渡，
被选句子由程序原样拷贝，不足自动补齐；实测值写入文章 frontmatter）。</p></td></tr>
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
 <b>{cfg.get('visuals.image_density',3)}</b>/5</td></tr>
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
</table>
<p><button class=primary>保存全部设置</button></p></div>
</form>

<div class=card><h3>⑥ AI · 凭证与分工</h3>
<p class=dim>本软件默认不含任何 API key——密钥由你添加，直接写入 macOS 钥匙串，不经过配置文件。任配一个即可运转，两个都配则互为备援。下面可以给每个 AI 环节分别指定用哪家：</p>
<form method=post style="margin-bottom:14px">
<input type=hidden name=_csrf value={CSRF}>
<input type=hidden name=form value=ai>
<table class=wrap>
<tr><td>整理成文用</td><td><select name=ai_article>
<option value=auto {dsel('auto', cfg.get('ai.article','auto'))}>自动（用已配置的，优先 OpenAI）</option>
<option value=openai {dsel('openai', cfg.get('ai.article','auto'))}>OpenAI</option>
<option value=anthropic {dsel('anthropic', cfg.get('ai.article','auto'))}>Anthropic (Claude)</option></select></td></tr>
<tr><td>截图召回用</td><td><select name=ai_visuals>
<option value=auto {dsel('auto', cfg.get('ai.visuals','auto'))}>自动</option>
<option value=openai {dsel('openai', cfg.get('ai.visuals','auto'))}>OpenAI</option>
<option value=anthropic {dsel('anthropic', cfg.get('ai.visuals','auto'))}>Anthropic (Claude)</option></select></td></tr>
<tr><td>问 AI 用</td><td><select name=ai_qa>
<option value=auto {dsel('auto', cfg.get('ai.qa','auto'))}>自动</option>
<option value=openai {dsel('openai', cfg.get('ai.qa','auto'))}>OpenAI</option>
<option value=anthropic {dsel('anthropic', cfg.get('ai.qa','auto'))}>Anthropic (Claude)</option></select></td></tr>
</table>
<p><button>保存分工</button></p></form>
<p class=dim>密钥状态：
 openai {'<span class=ok>已配置</span>' if _key_status('openai') else '<span class=bad>未配置</span>'} ·
 anthropic {'<span class=ok>已配置</span>' if _key_status('anthropic') else '<span class=bad>未配置</span>'}</p>
<form method=post>
<input type=hidden name=_csrf value={CSRF}><input type=hidden name=form value=keys>
<p><input type=password name=key_openai placeholder="OpenAI key（留空=不变）" style="width:60%"></p>
<p><input type=password name=key_anthropic placeholder="Anthropic key（留空=不变）" style="width:60%"></p>
<button>保存 key</button></form></div>
"""
    return page("设置", "settings", body)


def main(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    if open_browser:
        import threading, webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    print(f"{BRANDING} GUI → http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
