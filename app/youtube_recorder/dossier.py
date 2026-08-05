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

- observations（观点评价）：作者/主播对这家公司的看法、判断、评价
- concerns（关注点）：提到的风险、需要观察的点、担忧
- price_levels（推荐点位）：具体提到的价格、点位、目标价、支撑/阻力位、
  加仓/减仓的具体条件

每类可以有 0 条到多条，没有相关内容就是空数组。每条尽量简洁（一两句话），保留
原文里的具体数字/说法，不要编造原文没提到的信息。

只输出 JSON，不要任何其他文字：
{"observations": ["..."], "concerns": ["..."], "price_levels": ["..."]}
"""

SECTION_TITLES = {"observations": "观点评价", "concerns": "关注点",
                  "price_levels": "推荐点位"}


def article_plain_text(art: dict) -> str:
    """把 article.json 拼成给抽取 prompt 用的正文纯文本（标题+摘要+各节）。"""
    parts = [art.get("title_zh", ""), art.get("summary", "")]
    for s in art.get("sections", []) or []:
        parts.append(f"{s.get('heading', '')}\n{s.get('body', '')}")
    return "\n\n".join(p for p in parts if p)


def extract_company_points(cfg, con, video_id: str, company: str,
                           article_text: str) -> dict:
    """对一篇文章的正文跑一次抽取，返回
    {"observations": [...], "concerns": [...], "price_levels": [...]}。
    抽取失败（AI 报错/JSON 解析失败）时安静返回空结果，不阻塞主流程。"""
    user = f"公司/实体：{company}\n\n文章正文：\n{article_text}"
    try:
        reply = providers.complete(cfg, con, video_id, DOSSIER_SYSTEM, user,
                                   max_tokens=1200, purpose="dossier")
        data = providers.extract_json(reply)
    except Exception:
        return {"observations": [], "concerns": [], "price_levels": []}
    out = {}
    for key in SECTION_TITLES:
        v = data.get(key)
        out[key] = ([str(x).strip() for x in v if isinstance(x, str) and x.strip()]
                    if isinstance(v, list) else [])
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
                           published: str, source_link: str,
                           points: dict) -> bool:
    """把一篇文章抽取出的观点/关注点/点位追加进该公司的档案笔记（按公司名
    建档；文件已存在就在原文件基础上追加，不新建、不覆盖已有内容）。
    返回是否有实际写入（三类都是空的话不动笔记文件）。"""
    if not any(points.get(k) for k in SECTION_TITLES):
        return False
    path = dossier_note_path(vault_root, company)
    txt = path.read_text(encoding="utf-8") if path.exists() else _new_note(company)
    date = (published or db_now())[:10]
    txt = re.sub(r"(?m)^updated: .*$", f"updated: {db_now()[:10]}", txt, count=1)
    for key, title in SECTION_TITLES.items():
        for item in points.get(key) or []:
            line = f"- [{date}] {item}（来源：{source_link}）"
            txt = _append_under_heading(txt, title, line)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(txt, encoding="utf-8")
    tmp.replace(path)
    return True


# --- orchestration：一篇文章 -> 逐个未处理过的公司 ---------------------------

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


def process_video_companies(cfg, con, video_id: str, log=None) -> int:
    """处理一篇文章：找出它 companies 字段里还没抽取过的公司，逐个跑抽取
    并追加进对应的公司档案笔记，标记为已处理。返回实际处理的公司数。"""
    from .paths import work_dir
    from . import db as dbm
    aj = work_dir(video_id) / "article.json"
    if not aj.exists():
        return 0
    try:
        art = json.loads(aj.read_text(encoding="utf-8"))
    except Exception:
        return 0
    companies = [c for c in (art.get("companies") or [])[:6]
                if isinstance(c, str) and c.strip()]
    if not companies:
        return 0
    todo = dbm.dossier_unprocessed(con, video_id, companies)
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
    text = article_plain_text(art)
    done = 0
    for company in todo:
        points = extract_company_points(cfg, con, video_id, company, text)
        append_dossier_entries(vault_root, company, video_id=video_id,
                               published=published, source_link=source_link,
                               points=points)
        dbm.dossier_mark_processed(con, video_id, company)
        done += 1
        if log:
            log.event("dossier_processed", video_id=video_id, company=company)
    return done
