"""公司档案插件（默认关闭，见 config.yaml 的 dossier.enabled）。

从每篇新写入 vault 的文章正文里，抽取跟文章 companies 字段命中的每家
公司/实体相关的「观点评价」「关注点」「推荐点位」，写进按公司名建立的
独立笔记（50-公司档案/<公司>.md）——新信息增量追加进已有笔记，不新建、
不覆盖。每条都标注来源文章（Obsidian 内链），可回溯查证。

触发时机见 pipeline.py 的 _trigger_company_dossier()：文章写入 vault 后，
只要开关打开，就检查这篇文章提到的公司里还有哪些没处理过（db.py 的
dossier_processed 表按 (company, video_id) 去重），只处理新的，不重扫历史。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import BRANDING
from . import providers
from . import vault as _vault
from .db import now as db_now

DOSSIER_SYSTEM = """你是投资研究助理。给你一篇财经/科技视频整理稿的正文内容，和一个
你要重点关注的公司/实体名称。从这篇文章里，把所有跟这家公司直接相关的信息按下面
三类整理出来（只挑跟这家公司直接相关的内容，不要泛泛而谈整个市场或其他公司）：

- observations（观点评价）：作者/主播对这家公司的看法、判断、评价，字符串数组
- concerns（关注点）：提到的风险、需要观察的点、担忧，字符串数组
- price_levels（推荐点位）：每条是一个对象：
  {"text": "一两句话描述，保留原文具体数字/说法",
   "price": 具体数值(数字，没有就填 null),
   "level_type": "support|resistance|target|stop_loss|entry|exit|other"}
   （support=支撑位，resistance=压力/阻力位，target=目标价，
   stop_loss=止损位，entry=建议买入/加仓点，exit=建议卖出/止盈点，
   other=其他说不清类型但确实是这家公司自己股价/币价的点位）

price_levels 的判定要非常严格——price 字段必须是【这家公司自己的股票/
资产的可交易价格】，不是随便一个数字。下面这些情况【不要】算进
price_levels（哪怕原文里就在这家公司旁边提到，也不算）：
  · 涨跌幅百分比（"跌了20%"、"涨了三成"）——这是幅度，不是价格
  · 营收、AUM、市值、募资规模等"金额"（"营收68.2亿美元"、"管理规模500亿"）
    ——这是规模，不是每股/每单位价格
  · 市盈率、市销率等估值倍数（"市销率40倍"）——这是倍数，不是价格
  · 持股比例、解禁股数、用户数、发射次数、员工人数等计数类数字
  · CDS利差、basis point、大盘指数、VIX——跟这家公司股价本身无关的市场数字
  · 原文其实是在说【另一家公司/资产】的数字，只是顺带出现在同一段——
    这种要么忽略，要么按那家真正被谈论的公司单独归类（如果它也在你要
    关注的公司列表里的话），不能算到当前这家公司头上
  · 只是模糊描述、原文没给出具体数字的（这种 price 填 null 都不该出现，
    除非那句话本身很值得记录成一条无价位的点位描述）

拿不准一个数字是不是"这家公司自己的可交易价格"，宁可不放进 price_levels，
放进 observations 用文字描述就好——price_levels 里的每一条都会被画到
这家公司的股价走势图上，塞错东西比漏掉更糟。

每类可以有 0 条到多条，没有相关内容就是空数组。不要编造原文没提到的信息。

只输出 JSON，不要任何其他文字：
{"observations": ["..."], "concerns": ["..."],
 "price_levels": [{"text": "...", "price": null, "level_type": "other"}]}
"""

SECTION_TITLES = {"observations": "观点评价", "concerns": "关注点",
                  "price_levels": "推荐点位"}

LEVEL_TYPE_LABELS = {"support": "支撑位", "resistance": "压力位",
                     "target": "目标价", "stop_loss": "止损位",
                     "entry": "买入/加仓点", "exit": "卖出/止盈点",
                     "other": "点位"}


def article_plain_text(art: dict) -> str:
    """把 article.json 拼成给抽取 prompt 用的正文纯文本（标题+摘要+各节）。"""
    parts = [art.get("title_zh", ""), art.get("summary", "")]
    for s in art.get("sections", []) or []:
        parts.append(f"{s.get('heading', '')}\n{s.get('body', '')}")
    return "\n\n".join(p for p in parts if p)


def extract_company_points(cfg, con, video_id: str, company: str,
                           article_text: str) -> dict:
    """对一篇文章的正文跑一次抽取，返回
    {"observations": [...], "concerns": [...],
     "price_levels": [{"text","price","level_type"}, ...]}。
    抽取失败（AI 报错/JSON 解析失败）时安静返回空结果，不阻塞主流程。"""
    user = f"公司/实体：{company}\n\n文章正文：\n{article_text}"
    empty = {"observations": [], "concerns": [], "price_levels": []}
    try:
        reply = providers.complete(cfg, con, video_id, DOSSIER_SYSTEM, user,
                                   max_tokens=1500, purpose="dossier")
        data = providers.extract_json(reply)
    except Exception:
        return empty
    out = {}
    for key in ("observations", "concerns"):
        v = data.get(key)
        out[key] = ([str(x).strip() for x in v if isinstance(x, str) and x.strip()]
                    if isinstance(v, list) else [])
    levels = []
    raw_levels = data.get("price_levels")
    if isinstance(raw_levels, list):
        for item in raw_levels:
            if isinstance(item, str) and item.strip():
                levels.append({"text": item.strip(), "price": None,
                              "level_type": "other"})
            elif isinstance(item, dict) and str(item.get("text", "")).strip():
                price = item.get("price")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                lt = item.get("level_type")
                lt = lt if lt in LEVEL_TYPE_LABELS else "other"
                levels.append({"text": str(item["text"]).strip(), "price": price,
                              "level_type": lt})
    out["price_levels"] = levels
    return out


# --- vault 笔记：按公司名建档，增量追加 ------------------------------------

def dossier_dir(vault_root: Path) -> Path:
    d = vault_root / "50-公司档案"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dossier_note_path(vault_root: Path, company: str) -> Path:
    return dossier_dir(vault_root) / f"{_vault.safe_name(company, 60)}.md"


def _new_note(company: str) -> str:
    today = db_now()[:10]
    return f"""---
type: company-dossier
company: "{company}"
created: {today}
updated: {today}
generator: "{BRANDING}"
---

# {company}

## 观点评价

## 关注点

## 推荐点位
"""


def _append_under_heading(txt: str, heading: str, new_line: str) -> str:
    """在指定二级标题的小节末尾追加一行——同一小节里已有一模一样的内容
    就跳过（防止同一条被重复插入两次）。"""
    marker = f"## {heading}\n"
    idx = txt.find(marker)
    if idx == -1:
        return txt.rstrip("\n") + f"\n\n{marker}{new_line}\n"
    insert_at = idx + len(marker)
    next_idx = txt.find("\n## ", insert_at)
    next_idx = len(txt) if next_idx == -1 else next_idx + 1
    section = txt[insert_at:next_idx]
    if new_line.strip() and new_line.strip() in section:
        return txt
    section = section.rstrip("\n") + "\n" + new_line + "\n"
    return txt[:insert_at] + section + txt[next_idx:]


def append_dossier_entries(vault_root: Path, company: str, *, video_id: str,
                           published: str, channel: str | None,
                           source_link: str, points: dict, con=None) -> bool:
    """把一篇文章抽取出的观点/关注点/点位追加进该公司的档案笔记（按公司名
    建档；文件已存在就在原文件基础上追加，不新建、不覆盖已有内容）。每条
    都标注来源频道 + 可回链的文章笔记。传了 con 的话，推荐点位还会额外写
    一份结构化记录进 dossier_price_levels 表（供点位图/点位表用）。
    返回是否有实际写入（三类都是空的话不动笔记文件）。"""
    has_any = bool(points.get("observations") or points.get("concerns")
                  or points.get("price_levels"))
    if not has_any:
        return False
    path = dossier_note_path(vault_root, company)
    txt = path.read_text(encoding="utf-8") if path.exists() else _new_note(company)
    date = (published or db_now())[:10]
    txt = re.sub(r"(?m)^updated: .*$", f"updated: {db_now()[:10]}", txt, count=1)
    chan_prefix = f"{channel} · " if channel else ""
    obs_kind = {"observations": "observation", "concerns": "concern"}
    for key in ("observations", "concerns"):
        for item in points.get(key) or []:
            line = f"- [{date}] {item}（来源：{chan_prefix}{source_link}）"
            txt = _append_under_heading(txt, SECTION_TITLES[key], line)
            if con is not None:
                from . import db as dbm
                dbm.dossier_add_observation(
                    con, company=company, video_id=video_id, channel=channel,
                    mentioned_date=date, kind=obs_kind[key], text=item,
                    source_link=source_link)
    for lvl in points.get("price_levels") or []:
        text = lvl.get("text") if isinstance(lvl, dict) else str(lvl)
        if not text:
            continue
        line = f"- [{date}] {text}（来源：{chan_prefix}{source_link}）"
        txt = _append_under_heading(txt, SECTION_TITLES["price_levels"], line)
        if con is not None and isinstance(lvl, dict):
            from . import db as dbm
            dbm.dossier_add_price_level(
                con, company=company, video_id=video_id, channel=channel,
                mentioned_date=date, level_type=lvl.get("level_type") or "other",
                price=lvl.get("price"), raw_text=text, source_link=source_link)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(txt, encoding="utf-8")
    tmp.replace(path)
    return True


# --- 实体清理：合并别名 / 归档非实体（不做永久删除，只搬进归档子文件夹）----

def archive_dir(vault_root: Path) -> Path:
    d = dossier_dir(vault_root) / "_归档"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_entity_note(vault_root: Path, name: str) -> bool:
    """把一篇公司档案笔记移进归档子文件夹——不是删除，内容原样保留，只是
    不再出现在主列表里。用于"不是真正的公司/实体"的条目，或者别名合并后
    多出来的旧文件。真想删可以自己去归档文件夹里删。"""
    src = dossier_note_path(vault_root, name)
    if not src.exists():
        return False
    dst = archive_dir(vault_root) / src.name
    if dst.exists():
        stamp = db_now()[:19].replace(":", "").replace("-", "")
        dst = archive_dir(vault_root) / f"{src.stem}-{stamp}{src.suffix}"
    src.replace(dst)
    return True


def merge_entity_note(vault_root: Path, source_name: str, canonical_name: str) -> bool:
    """把 source_name 笔记里三个小节的内容原样合并进 canonical_name 笔记
    （去重同一模一样的行），然后把 source 笔记移进归档子文件夹。"""
    src = dossier_note_path(vault_root, source_name)
    if not src.exists() or source_name == canonical_name:
        return False
    dst = dossier_note_path(vault_root, canonical_name)
    dst_txt = dst.read_text(encoding="utf-8") if dst.exists() else _new_note(canonical_name)
    src_txt = src.read_text(encoding="utf-8")
    for title in SECTION_TITLES.values():
        marker = f"## {title}\n"
        idx = src_txt.find(marker)
        if idx == -1:
            continue
        insert_at = idx + len(marker)
        next_idx = src_txt.find("\n## ", insert_at)
        next_idx = len(src_txt) if next_idx == -1 else next_idx + 1
        for line in src_txt[insert_at:next_idx].splitlines():
            line = line.strip()
            if line.startswith("- ["):
                dst_txt = _append_under_heading(dst_txt, title, line)
    dst_txt = re.sub(r"(?m)^updated: .*$", f"updated: {db_now()[:10]}", dst_txt, count=1)
    tmp = dst.with_suffix(".md.tmp")
    tmp.write_text(dst_txt, encoding="utf-8")
    tmp.replace(dst)
    archive_entity_note(vault_root, source_name)
    return True


# --- orchestration：一篇文章 -> 逐个未处理过的公司 ---------------------------

# --- 股票代码解析 + 历史价格（点位图用） -----------------------------------

# 常见实体的手工映射表：省一次 AI 调用，也避免它偶尔认错。值为 None 表示
# "已知不是可查价格的上市标的"（比如未上市公司），跟"没配置过"区分开。
TICKER_MAP: dict[str, str | None] = {
    "英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT", "谷歌": "GOOGL",
    "亚马逊": "AMZN", "Meta": "META", "特斯拉": "TSLA", "台积电": "TSM",
    "英特尔": "INTC", "高通": "QCOM", "博通": "AVGO", "甲骨文": "ORCL",
    "IBM": "IBM", "AMD": "AMD", "ASML": "ASML", "德州仪器": "TXN",
    "美光科技": "MU", "SK Hynix": "000660.KS", "康宁": "GLW",
    "Palantir": "PLTR", "Salesforce": "CRM", "ServiceNow": "NOW",
    "PayPal": "PYPL", "Adobe": "ADBE", "SpaceX": None,
    "Robinhood": "HOOD", "SoFi": "SOFI", "Roblox": "RBLX", "Reddit": "RDDT",
    "联合健康": "UNH", "摩根大通": "JPM", "波音": "BA", "通用汽车": "GM",
    "可口可乐": "KO", "埃克森美孚": "XOM", "奈飞": "NFLX",
    "国巨": "2327.TW", "群创": "3481.TW", "聯電": "2303.TW",
    "南亚科": "2408.TW", "菲利普莫里斯国际": "PM", "诺基亚": "NOK",
    "阿斯利康": "AZN", "SOXL": "SOXL", "SOXX": "SOXX", "SPCX": "SPCX",
    "纳斯达克100": "^NDX", "Rocket Lab": "RKLB", "SanDisk": "SNDK",
    "标普500": "^GSPC", "S&P 500": "^GSPC", "IGV": "IGV", "XLK": "XLK",
    "XLF": "XLF", "QQQ": "QQQ", "SMH": "SMH",
}

# ── 指数级"动态市盈率"历史 ─────────────────────────────────────────────
# 这是雅虎给不了的东西：每个历史时点上市场当时的一致预期市盈率（彭博
# 终端里的 BEst P/E Ratio 1BF）。historyofmarket.com 把这几条序列免费
# 公开成静态 JSON（CC BY 4.0，注明出处即可），这是目前找到的唯一免费源。
#
# 注意 IGV（软件行业 ETF）没有对应序列——这个站只覆盖标普500 / 纳指100
# / 费城半导体 / 信息科技 / 金融这五条，软件行业级的一致预期市盈率暂时
# 没有免费来源。
_HOM_BASE = "https://historyofmarket.com/api"
BENCHMARK_SOURCES: dict[str, dict] = {
    "^GSPC": {"label": "标普500", "url": f"{_HOM_BASE}/sp500/forward-pe.json",
              "kind": "index"},
    "^NDX": {"label": "纳斯达克100", "url": f"{_HOM_BASE}/ndx/forward-pe.json",
             "kind": "index"},
    "SOXX": {"label": "费城半导体", "url": f"{_HOM_BASE}/sectors/forward-pe.json",
             "kind": "sector", "sector": "sox"},
    "^SOX": {"label": "费城半导体", "url": f"{_HOM_BASE}/sectors/forward-pe.json",
             "kind": "sector", "sector": "sox"},
    "XLK": {"label": "信息科技", "url": f"{_HOM_BASE}/sectors/forward-pe.json",
            "kind": "sector", "sector": "inft"},
    "XLF": {"label": "金融", "url": f"{_HOM_BASE}/sectors/forward-pe.json",
            "kind": "sector", "sector": "finl"},
}
# QQQ 就是跟踪纳指100 的 ETF，SMH/SOXL 跟半导体同源，估值上可以共用
BENCHMARK_SOURCES["QQQ"] = BENCHMARK_SOURCES["^NDX"]
BENCHMARK_SOURCES["SMH"] = BENCHMARK_SOURCES["SOXX"]
BENCHMARK_SOURCES["SOXL"] = BENCHMARK_SOURCES["SOXX"]

_HOM_UA = "YouTubeRecorder (personal research tool; historyofmarket.com CC-BY-4.0)"
_BENCH_CACHE: dict[str, tuple[float, dict]] = {}
_BENCH_TTL_SEC = 6 * 3600      # 这些序列是周频/月频，半天刷一次绰绰有余


def fetch_benchmark_forward_pe(ticker: str) -> dict:
    """取指数/板块级的"历史动态市盈率"曲线。

    返回 {"label":..., "series":[{"date","value"},...], "current": 数字|None,
    "trailing": [...] 或 [], "source_url":...}；没有对应序列就返回 {}。

    为什么必须用外部源：动态市盈率的历史值需要"当时"的分析师一致预期，
    雅虎只暴露当前这一个快照。这条数据平时是彭博/FactSet 的付费产品，
    historyofmarket.com 免费公开了标普500(1990起) / 纳指100(2001起) /
    费城半导体(2002起) / 信息科技(1998起) / 金融(1996起) 五条。
    软件行业（IGV）没有免费序列。"""
    import time
    src = BENCHMARK_SOURCES.get(ticker)
    if not src:
        return {}
    key = ticker
    hit = _BENCH_CACHE.get(key)
    if hit and time.time() - hit[0] < _BENCH_TTL_SEC:
        return hit[1]
    out: dict = {}
    try:
        import requests
        r = requests.get(src["url"], headers={"User-Agent": _HOM_UA}, timeout=20)
        r.raise_for_status()
        d = r.json()
        if src["kind"] == "sector":
            node = (d.get("sectors") or {}).get(src["sector"]) or {}
            series = node.get("series") or []
            trailing = []
        else:
            series = d.get("forward") or []
            trailing = d.get("trailing") or []
        series = [p for p in series if p.get("value") is not None]
        trailing = [p for p in trailing if p.get("value") is not None]
        if series:
            out = {
                "label": src["label"],
                "series": [{"date": p["date"], "value": round(float(p["value"]), 2)}
                           for p in series],
                "trailing": [{"date": p["date"], "value": round(float(p["value"]), 2)}
                             for p in trailing],
                "current": round(float(series[-1]["value"]), 2),
                "source_url": src["url"],
            }
    except Exception:
        out = {}
    _BENCH_CACHE[key] = (time.time(), out)
    return out


def benchmark_percentile(series: list[dict], value: float | None = None,
                         since: str = "2011-01-01") -> dict:
    """给一条动态市盈率序列算"当前处在历史什么位置"。

    since 默认 2011 是有原因的：2002~2010 年半导体（以及金融在 2008~2012）
    的总盈利多次逼近或跌破零，除出来的市盈率会飙到一两百倍甚至断档，那
    不是"大家真的付了这个价"，而是分母趋近于零的除法爆炸。拿它算均值和
    分位数会把结论带偏，所以默认只用 2011 之后这段干净的样本。"""
    pts = [p for p in series if p["date"] >= since]
    if not pts:
        pts = series
    if not pts:
        return {}
    vals = sorted(p["value"] for p in pts)
    cur = value if value is not None else series[-1]["value"]
    below = sum(1 for v in vals if v <= cur)
    return {
        "n": len(vals), "since": pts[0]["date"],
        "min": vals[0], "max": vals[-1],
        "median": vals[len(vals) // 2],
        "mean": round(sum(vals) / len(vals), 2),
        "current": cur,
        "pct": round(100.0 * below / len(vals), 1),
    }


def _ai_resolve_ticker(cfg, con, name: str) -> str | None:
    system = ("给你一个公司/实体的名字，回答它在雅虎财经(Yahoo Finance)上的股票/ETF/"
             "指数代码。只输出代码本身（比如 AAPL、2330.TW、000660.KS、^NDX），不要"
             "任何其他文字、不要解释。如果它根本不是一个可以查到历史价格的上市标的"
             "（比如未上市公司、人物、概念），只输出 NONE。")
    try:
        reply = providers.complete(cfg, con, "dossier-ticker-lookup", system, name,
                                   max_tokens=20, purpose="dossier")
    except Exception:
        return None
    reply = (reply or "").strip().strip("`").strip('"')
    reply = reply.split()[0] if reply else ""
    if not reply or reply.upper() == "NONE":
        return None
    return reply


def resolve_ticker(cfg, con, name: str) -> str | None:
    """给一个 canonical 实体名，返回雅虎财经代码（查不到就是 None）。结果
    缓存进 dossier_entities.ticker，同一个名字只真正查一次。"""
    from . import db as dbm
    row = dbm.dossier_get_entity(con, name)
    if row is not None and row["ticker"] is not None:
        return row["ticker"] or None
    if name in TICKER_MAP:
        ticker = TICKER_MAP[name]
    else:
        ticker = _ai_resolve_ticker(cfg, con, name)
    dbm.dossier_register_entity(con, name)
    con.execute("UPDATE dossier_entities SET ticker=? WHERE name=?",
               (ticker or "", name))
    con.commit()
    return ticker


_PRICE_HISTORY_CACHE: dict[str, tuple[float, list[dict]]] = {}
_PRICE_HISTORY_TTL_SEC = 900  # 15 分钟，避免同一次浏览反复打雅虎财经


def fetch_price_history(ticker: str, period: str = "5y") -> list[dict]:
    """返回 [{"date": "YYYY-MM-DD", "close": 数字}, ...]，取不到（网络问题/
    代码无效）就安静返回空列表——图表那边会优雅降级成只显示点位表。默认拉
    5 年，配合前端"近5年/全部"两档快速筛选按钮用。"""
    import time
    cache_key = f"{ticker}|{period}"
    hit = _PRICE_HISTORY_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _PRICE_HISTORY_TTL_SEC:
        return hit[1]
    out: list[dict] = []
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=period)
        if hist is not None and not hist.empty:
            for idx, row in hist.iterrows():
                close = row.get("Close")
                if close is None:
                    continue
                out.append({"date": idx.strftime("%Y-%m-%d"),
                           "close": round(float(close), 4)})
    except Exception:
        out = []
    _PRICE_HISTORY_CACHE[cache_key] = (time.time(), out)
    return out


_VALUATION_CACHE: dict[str, tuple[float, dict]] = {}
_VALUATION_TTL_SEC = 3600  # 1 小时——估值指标变化慢，不用跟价格一样勤


def fetch_valuation(ticker: str) -> dict:
    """返回 {"forward_pe": 数字|None, "trailing_pe": 数字|None,
    "forward_eps": 数字|None}。数据来自雅虎财经（yfinance，本来就是依赖，
    不需要额外的 API key，也不花钱）。

    覆盖情况实测：美股个股、台股、韩股的普通股票基本都有 forwardPE；
    ETF 一般只有 trailingPE 没有 forwardPE（因为没有"一致预期 EPS"这个
    概念）；指数（^NDX 之类）两个都没有；成交太清淡的小票也可能没有。
    取不到就返回 None，页面那边优雅降级不显示，不报错。"""
    import time
    hit = _VALUATION_CACHE.get(ticker)
    if hit and time.time() - hit[0] < _VALUATION_TTL_SEC:
        return hit[1]
    out = {"forward_pe": None, "trailing_pe": None, "forward_eps": None}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}

        def _num(v):
            # 雅虎偶尔回负数/离谱大的 PE（盈利接近 0 时的除法爆炸），
            # 这种数字没有解读价值，直接当作没有
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f <= 0 or f > 1000:
                return None
            return round(f, 2)

        out["forward_pe"] = _num(info.get("forwardPE"))
        out["trailing_pe"] = _num(info.get("trailingPE"))
        try:
            fe = float(info.get("forwardEps"))
            out["forward_eps"] = round(fe, 4)
        except (TypeError, ValueError):
            out["forward_eps"] = None
    except Exception:
        pass
    _VALUATION_CACHE[ticker] = (time.time(), out)
    return out


_PE_HISTORY_CACHE: dict[str, tuple[float, list[dict]]] = {}
_PE_HISTORY_TTL_SEC = 3600
# 自算的最新市盈率必须落在雅虎自己报的 trailingPE 的这个倍数区间内，
# 否则认为我们的 EPS 口径和价格对不上（最典型的是 ADR：价格是美元、
# 财报 EPS 是当地货币，比如台积电算出来会是 1.25 倍而不是 36 倍），
# 这种情况宁可不画，也不能画一条错的线。
_PE_SANITY_LO, _PE_SANITY_HI = 0.5, 2.0


def _annual_eps_timeline(ticker: str) -> list[tuple[str, float]]:
    """[(财年结束日, 稀释EPS), ...] 由旧到新。雅虎只给 4~5 个年度。"""
    import math
    try:
        import yfinance as yf
        a = yf.Ticker(ticker).income_stmt
    except Exception:
        return []
    if a is None or getattr(a, "empty", True):
        return []
    row = next((r for r in a.index if str(r) == "Diluted EPS"), None)
    if row is None:
        return []
    out: list[tuple[str, float]] = []
    for col in a.columns:
        try:
            v = float(a.loc[row, col])
        except (TypeError, ValueError):
            continue
        if math.isnan(v):
            continue
        out.append((str(col)[:10], round(v, 4)))
    out.sort()
    return out


def fetch_pe_history(ticker: str, history: list[dict],
                     trailing_pe: float | None = None) -> list[dict]:
    """按 收盘价 ÷ 最近一个已公布财年的稀释 EPS 算一条静态市盈率历史线，
    返回 [{"date":..., "pe":...}, ...]。

    为什么是"最近已公布财年 EPS"而不是真正的 TTM：雅虎免费接口只给 4~5
    个季度的季度 EPS，凑不出足够多的滚动四季度点来画线；年度 EPS 有 4~5
    年，配上日线价格就能画出一条像样的曲线，代价是 EPS 在财年边界上是
    跳变的（所以线上会有台阶，这是真实的口径限制，不是 bug）。

    亏损年份（EPS<=0）没有市盈率可言，直接跳过，线会断开而不是硬连。

    最后有一道体检：把自算的最新值和雅虎自己报的 trailingPE 比一比，差
    出 2 倍以上就整条不返回。这道闸主要挡的是 ADR 的货币错配——台积电
    价格按美元、EPS 按台币，算出来是 1.25 倍而不是 36 倍——与其画一条
    错的线，不如不画。"""
    import time
    if not history:
        return []
    cache_key = f"{ticker}|{history[-1]['date']}|{trailing_pe}"
    hit = _PE_HISTORY_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _PE_HISTORY_TTL_SEC:
        return hit[1]

    eps = _annual_eps_timeline(ticker)
    out: list[dict] = []
    if eps:
        for h in history:
            d, close = h["date"], h["close"]
            e = None
            for period_end, val in eps:
                if period_end <= d:
                    e = val
            if e is None or e <= 0:
                continue
            pe = close / e
            if 0 < pe < 1000:
                out.append({"date": d, "pe": round(pe, 2)})
    # 体检：口径对不上就整条丢掉
    if out and trailing_pe:
        ratio = out[-1]["pe"] / trailing_pe
        if not (_PE_SANITY_LO <= ratio <= _PE_SANITY_HI):
            out = []
    _PE_HISTORY_CACHE[cache_key] = (time.time(), out)
    return out


_OBS_LINE_RE = re.compile(
    r"^- \[(\d{4}-\d{2}-\d{2})\] (.*?)（来源：(?:(.+?) · )?\[\[([^\]]+)\]\]）$")


def backfill_observations_from_notes(vault_root: Path, con, log=None) -> dict:
    """一次性把现有档案笔记里"观点评价"/"关注点"小节里已经写好的内容解析
    回 dossier_observations 结构化表——这张表是这一轮才加的，之前笔记里的
    内容本来就有，只是没同步进结构化表，导致"AI 近期总结"对已有历史内容
    显示"没有内容"。不调用 AI，纯本地文本解析（笔记里每一行的格式是
    append_dossier_entries() 自己写的，是固定格式，可以稳定解析回去）。
    已经存在的 (company, video_id, kind, text) 组合会跳过，可以安全重复
    跑，不会出现重复条目。"""
    from . import db as dbm
    d = dossier_dir(vault_root)
    scanned_notes = 0
    added = 0
    skipped_existing = 0
    unparsed = 0
    for p in sorted(d.glob("*.md")):
        if p.parent != d:
            continue
        scanned_notes += 1
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m_company = re.search(r'(?m)^company: "?([^"\n]*)"?', txt)
        company = (m_company.group(1).strip() if m_company else p.stem)
        for key, kind in (("observations", "observation"), ("concerns", "concern")):
            heading = SECTION_TITLES[key]
            marker = f"## {heading}\n"
            idx = txt.find(marker)
            if idx == -1:
                continue
            start = idx + len(marker)
            next_idx = txt.find("\n## ", start)
            section = txt[start:next_idx if next_idx != -1 else len(txt)]
            for line in section.splitlines():
                line = line.strip()
                if not line.startswith("- ["):
                    continue
                m = _OBS_LINE_RE.match(line)
                if not m:
                    unparsed += 1
                    continue
                date, item_text, channel, stem = m.groups()
                video_id = stem.rsplit("--", 1)[1] if "--" in stem else None
                if not video_id:
                    unparsed += 1
                    continue
                exists = con.execute(
                    "SELECT 1 FROM dossier_observations WHERE company=? AND "
                    "video_id=? AND kind=? AND text=? LIMIT 1",
                    (company, video_id, kind, item_text)).fetchone()
                if exists:
                    skipped_existing += 1
                    continue
                dbm.dossier_add_observation(
                    con, company=company, video_id=video_id, channel=channel,
                    mentioned_date=date, kind=kind, text=item_text,
                    source_link=f"[[{stem}]]")
                added += 1
        if log:
            log.event("dossier_backfill_observations_note", company=company)
    return {"scanned_notes": scanned_notes, "added": added,
           "skipped_existing": skipped_existing, "unparsed": unparsed}


SUMMARY_SYSTEM = """你是投资研究助理。给你某家公司/实体最近一段时间、按时间从旧到新
排列的多篇财经视频里提到的观点评价、关注点、推荐点位（每条都标了发布日期和来源频道）。
帮我写一份"近期总结"，汇总各路主播/分析师对这家公司的看法，给我参考。

写作要求：
- 只能围绕这家公司本身，不要延伸去总结大盘走势或者其他公司
- 时间越新的内容权重越高，要重点体现在总结里；时间越久远的内容只需要一笔
  带过（提一下当时的背景/看法），不要展开细节——总结应该读起来像是"最新
  情况是这样，此前大家怎么看"，而不是不分时间地平铺直叙
- 不同来源如果观点有分歧或矛盾，直接指出来（比如"A频道认为...，但B频道
  觉得..."），不要含糊带过、不要强行统一成一个结论
- 提到具体点位/数字时保留原始数字，不要模糊化
- 写成 100~250 字左右的中文自然语言段落（可以分两段），不要用列表、
  不要加标题
- 只输出总结正文，不要引号、不要任何其他说明文字
"""


def generate_dossier_summary(cfg, con, company: str) -> dict | None:
    """生成/刷新某家公司的"AI 近期总结"：取最近三个月以内、最多 10 篇视频
    的观点评价/关注点/推荐点位（三个月和十篇哪个覆盖视频数更少就用哪个，
    见 db.dossier_observations_for_company），按时间从旧到新拼给 AI，
    汇总各家看法，写进 dossier_summaries。没有可用内容就返回 None（不产生
    空总结、不覆盖已有的旧总结）。"""
    from . import db as dbm
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    obs_rows = dbm.dossier_observations_for_company(con, company, since=cutoff,
                                                     limit_videos=10)
    if not obs_rows:
        return None
    video_ids = {r["video_id"] for r in obs_rows}
    price_rows = [r for r in dbm.dossier_price_levels_for(con, company)
                 if r["video_id"] in video_ids]

    by_video: dict[str, dict] = {}
    order: list[str] = []
    for r in obs_rows:
        vid = r["video_id"]
        if vid not in by_video:
            by_video[vid] = {"date": r["mentioned_date"] or "",
                             "channel": r["channel"] or "未知频道", "items": []}
            order.append(vid)
        label = "观点" if r["kind"] == "observation" else "关注点"
        by_video[vid]["items"].append(f"[{label}] {r['text']}")
    for r in price_rows:
        vid = r["video_id"]
        if vid not in by_video:
            continue
        price_txt = f"（{r['price']}）" if r["price"] is not None else ""
        by_video[vid]["items"].append(
            f"[{LEVEL_TYPE_LABELS.get(r['level_type'], '点位')}{price_txt}] {r['raw_text']}")
    order.sort(key=lambda v: (by_video[v]["date"], v))

    blocks = []
    for vid in order:
        v = by_video[vid]
        items_text = "\n".join(f"  - {i}" for i in v["items"])
        blocks.append(f"{v['date']}（{v['channel']}）：\n{items_text}")
    user = f"公司/实体：{company}\n\n" + "\n\n".join(blocks)

    try:
        reply = providers.complete(cfg, con, "dossier_summary", SUMMARY_SYSTEM, user,
                                   max_tokens=800, purpose="dossier")
    except Exception:
        return None
    summary = (reply or "").strip().strip('"').strip()
    if not summary:
        return None
    dbm.dossier_set_summary(con, company, summary, len(order))
    return {"summary": summary, "item_count": len(order)}


def filter_price_level_outliers(levels, reference: float | None):
    """按参考价格（通常是最新收盘价，没有收盘价就退回点位自身的中位数）
    过滤掉明显跑偏的点位——价格是参考价 20 倍以上、或者不到参考价的
    1/20，基本可以确定是抽取时张冠李戴（比如把视频里提到的别的数字/别的
    公司的点位算到了这家头上），图表和表格里都不应该展示，但不动数据库
    本身（用户想恢复的话可以整体重扫）。返回 (保留的点位, 被排除的点位)。"""
    dated = [r for r in levels if r["price"] is not None]
    ref = reference
    if not ref and dated:
        prices = sorted(r["price"] for r in dated)
        ref = prices[len(prices) // 2]
    if not ref:
        return list(levels), []
    lo, hi = ref / 20.0, ref * 20.0
    kept, excluded = [], []
    for r in levels:
        p = r["price"]
        if p is not None and not (lo <= p <= hi):
            excluded.append(r)
        else:
            kept.append(r)
    return kept, excluded


# 同一天内两条点位价差在这个比例以内，视为"同一个位置"可以合并显示——
# 技术分析里支撑/压力位本来就不是一个精确数字，2% 以内的差异基本就是
# 同一个位置的不同说法（比如一个说 150、一个说 152），再往上比如 5%、
# 10% 往往已经是不同的位置了，合并反而会抹掉有意义的区别，所以选了一个
# 比较保守的阈值。
PRICE_CLUSTER_PCT = 0.02


def cluster_nearby_price_levels(levels, pct_threshold: float = PRICE_CLUSTER_PCT):
    """把同一天(mentioned_date 完全相同)、价格相近(价差 <= pct_threshold *
    价格)的点位聚成一簇，图表上合并成一个点显示，避免同一天挤出好几个
    几乎一样的点位、看着乱。只影响图表怎么画，不改数据库、不改下面的
    点位表格——表格还是逐条列出，方便单独删除。

    没有价格的点位（price is None）各自单独一簇，不参与聚类（没数字没法
    比"相近"）。按价格从低到高排序后贪心聚类：跟"这一簇里最低价"的差
    在阈值内就并进来，一超过阈值就另起一簇——用簇内最低价当基准而不是
    逐个比较上一条，是为了避免"A 跟 B 近、B 跟 C 近，但 A 跟 C 已经差
    很远"这种链式漂移把明显不同的两个位置也粘到一起。

    返回一个"簇"的列表，每个簇是原始点位行的列表（长度 1 = 没有能合并
    的搭档，长度 >1 = 合并了）。"""
    from collections import defaultdict
    by_date: dict[str, list] = defaultdict(list)
    for r in levels:
        by_date[r["mentioned_date"] or ""].append(r)
    clusters = []
    for rows in by_date.values():
        priced = sorted((r for r in rows if r["price"] is not None),
                        key=lambda r: r["price"])
        unpriced = [r for r in rows if r["price"] is None]
        cur: list = []
        for r in priced:
            if cur and r["price"] - cur[0]["price"] <= pct_threshold * cur[0]["price"]:
                cur.append(r)
            else:
                if cur:
                    clusters.append(cur)
                cur = [r]
        if cur:
            clusters.append(cur)
        clusters.extend([r] for r in unpriced)
    return clusters


def channel_name_for_video(con, video_id: str) -> str | None:
    row = con.execute(
        "SELECT c.name FROM videos v JOIN channels c ON c.channel_id=v.channel_id "
        "WHERE v.video_id=?", (video_id,)).fetchone()
    name = row["name"] if row else None
    return name or None


def rescan_all(cfg, con, log=None) -> dict:
    """全量重新扫描：不是增量补跑，是彻底重来一遍。用于抽取格式升级之后
    （比如加了频道前缀、结构化点位）让所有历史数据补齐新字段——不是简单
    在旧内容后面追加（追加会导致新旧两份措辞不同但内容重复的条目堆在一
    起），而是：

    1. 把当前每一篇 canonical 公司笔记都归档（不删除，挪进 _归档/，文件名
       带时间戳，不会跟之前清理时归档的旧文件冲突）；
    2. 清空 dossier_processed（去重记录）和 dossier_price_levels（结构化
       点位），这样每个 (公司, 视频) 都会被当成没处理过，重新走一遍抽取；
    3. 调用 backfill_all() 重新生成——笔记从空白重新写起，所有条目都带
       上当前版本的格式（频道前缀、结构化点位落进新表）。

    dossier_entities 里的批准状态/别名/股票代码缓存不受影响，只重来"内容"
    本身。"""
    from . import db as dbm
    vault_root = cfg.vault_root
    if not vault_root:
        return {"archived_notes": 0, "scanned": 0, "videos_with_companies": 0,
               "companies_processed": 0}
    canonical_names = [r["name"] for r in con.execute(
        "SELECT name FROM dossier_entities WHERE canonical IS NULL")]
    # 也把还没被任何 dossier_entities 行覆盖、但已经有笔记文件的旧条目算上
    d = dossier_dir(vault_root)
    for p in d.glob("*.md"):
        if p.parent == d and p.stem not in canonical_names:
            canonical_names.append(p.stem)
    archived = 0
    for name in canonical_names:
        if archive_entity_note(vault_root, name):
            archived += 1
    con.execute("DELETE FROM dossier_processed")
    con.execute("DELETE FROM dossier_price_levels")
    con.commit()
    if log:
        log.event("dossier_rescan_reset", archived_notes=archived)
    result = backfill_all(cfg, con, log)
    result["archived_notes"] = archived
    return result


def backfill_all(cfg, con, log=None) -> dict:
    """手动补跑：把库里所有已经写过 wiki 笔记的历史文章都过一遍公司档案
    抽取——用于给插件"喂"存量数据，或者抽取逻辑升级后重新跑一遍。跟
    pipeline._trigger_company_dossier() 那个只处理"这一轮新写入"的自动
    钩子是两回事；复用同一套 (company, video_id) 去重，不会重复处理。"""
    rows = con.execute(
        "SELECT DISTINCT video_id FROM writes WHERE note_kind='wiki'").fetchall()
    video_ids = [r["video_id"] for r in rows]
    scanned = 0
    videos_with_companies = 0
    companies_processed = 0
    for vid in video_ids:
        scanned += 1
        try:
            n = process_video_companies(cfg, con, vid, log)
        except Exception as e:
            if log:
                log.event("dossier_backfill_video_failed", video_id=vid, detail=str(e))
            continue
        if n:
            videos_with_companies += 1
            companies_processed += n
        if log:
            log.event("dossier_backfill_progress", video_id=vid, companies=n)
    return {"scanned": scanned, "videos_with_companies": videos_with_companies,
           "companies_processed": companies_processed}


# ── 指数/ETF 提及扫描 ─────────────────────────────────────────────────
# 问题：article.json 的 companies 字段是 AI 在写文章时顺手标的"文章提到
# 的公司"，抽取 prompt（article.py COMPOSE_SYSTEM）举的例子都是具体公司
# （英伟达/TSLA/鲍威尔），没有强调过指数/ETF，所以历史上很多篇明明提到
# "标普500""纳指""费城半导体"的文章，companies 字段里根本没写这些名字
# ——process_video_companies() 只处理 companies 字段里有的名字，这些提及
# 就被漏掉了，指数页面因此一直是空的。
#
# 解决办法分两半：
# 1) 这里：正则扫一遍所有历史文章正文，命中就当场跑一次抽取（复用
#    extract_company_points，跟正常流程一模一样，就是不依赖 companies
#    字段触发）——这一步要花真的 AI 调用，跟"零 AI 成本"的笔记回填
#    不是一回事，但没有更省的办法：文章里具体说了什么、值不值得记一条，
#    只有让 AI 读一遍正文才能判断。
# 2) article.py 那边加了指数/ETF 的例子到 companies 抽取 prompt 里，
#    以后新文章会自己把这些标进 companies 字段，不用再靠这里的正则扫。
def _word(token: str) -> str:
    """给一个纯 ASCII 代码词（NDX/QQQ 之类）套上"前后不是字母数字"的边界。
    不用 \\b：Python re 把 CJK 字符也算进 \\w，"到QQQ的"这种中文夹着的写法
    \\b 反而不触发（两边都是 \\w，没有边界），得手写 lookaround 才行。"""
    return rf"(?<![A-Za-z0-9]){token}(?![A-Za-z0-9])"


INDEX_ALIASES: dict[str, list[str]] = {
    "标普500": [r"标普\s*500", r"标普500指数", r"S&P\s*500", _word("SPX"),
              r"\^GSPC"],
    "纳斯达克100": [r"纳斯达克\s*100", r"纳指\s*100", r"纳斯达克指数",
                r"纳指(?!\d)", r"Nasdaq\s*100", _word("NDX"), _word("QQQ")],
    "SOXX": [r"费城半导体", r"半导体指数", _word("SOX(?!L)"), _word("SOXX")],
    "IGV": [_word("IGV"), r"软件行业\s*ETF", r"软件\s*ETF"],
}
_INDEX_ALIAS_RE = {name: re.compile("|".join(pats), re.I)
                   for name, pats in INDEX_ALIASES.items()}


def scan_video_for_index_mentions(cfg, con, video_id: str, log=None) -> int:
    """对一篇文章：正则命中的每个指数/ETF（还没为这篇文章跑过的）都单独
    抽一次，追加进对应指数的档案笔记。返回新处理的指数数。用法跟
    process_video_companies 一致，是它的补充，不是替代——那边继续负责
    companies 字段里的普通公司，这里专门兜底指数/ETF 这一类。"""
    from .paths import work_dir
    from . import db as dbm
    aj = work_dir(video_id) / "article.json"
    if not aj.exists():
        return 0
    try:
        art = json.loads(aj.read_text(encoding="utf-8"))
    except Exception:
        return 0
    text = article_plain_text(art)
    if not text:
        return 0
    matched = [name for name, pat in _INDEX_ALIAS_RE.items() if pat.search(text)]
    if not matched:
        return 0
    todo = dbm.dossier_unprocessed(con, video_id, matched)
    if not todo:
        return 0
    vault_root = cfg.vault_root
    if not vault_root:
        return 0
    row = con.execute(
        "SELECT note_path FROM writes WHERE video_id=? AND note_kind='wiki' "
        "ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
    if not row:
        return 0
    source_link = f"[[{Path(row['note_path']).stem}]]"
    video_row = con.execute(
        "SELECT published_at FROM videos WHERE video_id=?", (video_id,)).fetchone()
    published = video_row["published_at"] if video_row else ""
    channel = channel_name_for_video(con, video_id)
    done = 0
    for name in todo:
        points = extract_company_points(cfg, con, video_id, name, text)
        append_dossier_entries(vault_root, name, video_id=video_id,
                               published=published, channel=channel,
                               source_link=source_link, points=points, con=con)
        dbm.dossier_mark_processed(con, video_id, name)
        done += 1
        if log:
            log.event("dossier_index_scan_processed", video_id=video_id, index=name)
    return done


def backfill_index_mentions(cfg, con, log=None) -> dict:
    """全库扫一遍：找出所有提到过标普500/纳斯达克100/SOXX/IGV这几个指数/
    ETF、但从没为这篇文章单独抽取过的历史文章，逐篇补跑。幂等——已经
    处理过的 (指数, video_id) 组合会被 dossier_unprocessed 自动跳过，
    可以放心重复执行。"""
    rows = con.execute(
        "SELECT DISTINCT video_id FROM writes WHERE note_kind='wiki'").fetchall()
    scanned = 0
    videos_matched = 0
    entries_added = 0
    for r in rows:
        scanned += 1
        try:
            n = scan_video_for_index_mentions(cfg, con, r["video_id"], log)
        except Exception as e:
            if log:
                log.event("dossier_index_backfill_failed",
                         video_id=r["video_id"], detail=str(e))
            continue
        if n:
            videos_matched += 1
            entries_added += n
    return {"scanned": scanned, "videos_matched": videos_matched,
           "entries_added": entries_added}


def process_video_companies(cfg, con, video_id: str, log=None) -> int:
    """处理一篇文章：解析它 companies 字段里每个原始名字对应的 canonical
    实体（新名字自动以 pending 状态登记，见 db.dossier_resolve_entity），
    跳过已经标记为"不是实体"的名字，把还没为这篇文章跑过抽取的 canonical
    实体逐个抽取、追加进对应档案笔记（不管是 pending 还是 approved 都会
    正常计算——批不批准只影响 GUI 里怎么展示，不影响这里的抽取）。
    返回实际处理的 canonical 实体数。"""
    from .paths import work_dir
    from . import db as dbm
    aj = work_dir(video_id) / "article.json"
    if not aj.exists():
        return 0
    try:
        art = json.loads(aj.read_text(encoding="utf-8"))
    except Exception:
        return 0
    raw_companies = [c for c in (art.get("companies") or [])[:6]
                     if isinstance(c, str) and c.strip()]
    if not raw_companies:
        return 0
    canonical_companies: list[str] = []
    for raw in raw_companies:
        info = dbm.dossier_resolve_entity(con, raw)
        if info["status"] == "rejected":
            continue
        if info["canonical"] not in canonical_companies:
            canonical_companies.append(info["canonical"])
    if not canonical_companies:
        return 0
    todo = dbm.dossier_unprocessed(con, video_id, canonical_companies)
    if not todo:
        return 0
    vault_root = cfg.vault_root
    if not vault_root:
        return 0
    row = con.execute(
        "SELECT note_path FROM writes WHERE video_id=? AND note_kind='wiki' "
        "ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
    if not row:
        return 0
    source_link = f"[[{Path(row['note_path']).stem}]]"
    video_row = con.execute(
        "SELECT published_at FROM videos WHERE video_id=?", (video_id,)).fetchone()
    published = video_row["published_at"] if video_row else ""
    channel = channel_name_for_video(con, video_id)
    text = article_plain_text(art)
    done = 0
    for company in todo:
        points = extract_company_points(cfg, con, video_id, company, text)
        append_dossier_entries(vault_root, company, video_id=video_id,
                               published=published, channel=channel,
                               source_link=source_link, points=points, con=con)
        dbm.dossier_mark_processed(con, video_id, company)
        done += 1
        if log:
            log.event("dossier_processed", video_id=video_id, company=company)
        # 置顶公司（比如自己持有的）：有新内容进来就自动刷新 AI 近期总结；
        # 非置顶的公司留给用户在页面上手动点刷新，省得每篇新文章都触发一次
        # AI 调用。总结生成失败（AI 报错等）不该打断抽取主流程，安静跳过。
        ent = dbm.dossier_get_entity(con, company)
        if ent is not None and ent["pinned"]:
            try:
                generate_dossier_summary(cfg, con, company)
            except Exception as e:
                if log:
                    log.event("dossier_summary_autogen_failed", company=company,
                             detail=str(e))
    return done
