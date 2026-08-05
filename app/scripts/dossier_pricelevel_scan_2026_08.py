"""一次性执行：扫描 dossier_price_levels 里所有已抽取的"推荐点位"，用 AI
判断每一条是不是真的在说这家公司自己股票/资产的可交易价位——很多条其实
是视频里顺带提到的别的数字（百分比、CDS 利差、基金规模 AUM、持股比例、
大盘/VIX 点位、别的公司的价格），被旧版抽取逻辑一股脑塞进了 level_type
='other' 里。判定为"不是"的直接从表里删掉（这是抽取出来的衍生数据，删了
可以靠 dossier-rescan 重新生成，不受"不能永久删除用户数据"的约束——那条
规则针对的是用户自己的文件/邮件/消息，不是这类可重算的派生索引数据）。

跑法：
    /usr/local/bin/python3 scripts/dossier_pricelevel_scan_2026_08.py [--apply]
不带 --apply 只打印将要删除的条数和示例，方便先看一眼判断是否离谱；
带 --apply 才真正执行删除。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from youtube_recorder import config as cfg_mod  # noqa: E402
from youtube_recorder import db as dbm          # noqa: E402
from youtube_recorder import providers          # noqa: E402

BATCH_SIZE = 30

SYSTEM = """你是金融数据质检员。给你一批「公司名 + 一条从财经视频里抽取出来的
"推荐点位"记录」，每条记录包含 raw_text（原文摘录）、price（抽取出的数字，
可能是 null）、level_type。

你要判断：这条记录里的 price 数字，是不是真的在描述【这家公司自己的股票/
资产的可交易价格点位】（比如支撑位、压力位、目标价、止损位、买入点、卖出点）。

判定为"不是"（drop）的常见情况，包括但不限于：
- 百分比数字（持股占比、涨跌幅、仓位比例、去杠杆比例等），不是价格
- CDS 利差、basis point（基点）数字
- 基金/ETF 管理规模 AUM（"XX 亿美元"这类规模数字，不是价格）
- 大盘指数、VIX、期货贴水等跟这家公司本身股价无关的市场数字
- 原文其实是在说【另一家公司/资产】的价格，只是顺带出现在同一段落里
- price 是 null 且原文根本没有具体数字，只是模糊描述（没有价格可判断）
- 日期、年份、股数、员工人数等其他跟价格无关的数字

判定为"是"（keep）的情况：
- 原文明确说这家公司的股价/币价/资产价格到了、涨到、跌到、突破、支撑在、
  压力在某个具体数字，且这个数字就是 price 字段对应的那个价
- 哪怕 level_type 是 other，只要那个数字确实是这家公司自己股价的某个
  点位（哪怕类型不好归类），也算 keep

只输出 JSON，不要任何其他文字，格式：
{"verdicts": [{"id": 123, "verdict": "keep|drop", "reason": "一两个词说明原因"}]}
"""


def classify_batch(cfg, rows: list[dict]) -> dict:
    payload = [
        {"id": r["id"], "company": r["company"], "level_type": r["level_type"],
         "price": r["price"], "raw_text": r["raw_text"]}
        for r in rows
    ]
    user = json.dumps(payload, ensure_ascii=False)
    reply = providers.complete(cfg, None, "dossier_pricelevel_scan", SYSTEM, user,
                               max_tokens=3000, purpose="dossier")
    data = providers.extract_json(reply)
    out = {}
    for v in data.get("verdicts", []):
        try:
            out[int(v["id"])] = (v.get("verdict"), v.get("reason", ""))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def main() -> None:
    apply = "--apply" in sys.argv
    cfg = cfg_mod.load()
    con = dbm.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT id, company, channel, mentioned_date, level_type, price, raw_text "
        "FROM dossier_price_levels ORDER BY id")]
    print(f"total rows: {len(rows)}")

    all_verdicts: dict[int, tuple[str, str]] = {}
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        try:
            verdicts = classify_batch(cfg, batch)
        except Exception as e:
            print(f"batch {i}-{i+len(batch)}: FAILED ({e}), skipping (kept as-is)")
            continue
        all_verdicts.update(verdicts)
        print(f"batch {i}-{i+len(batch)}: classified {len(verdicts)}/{len(batch)}")

    by_id = {r["id"]: r for r in rows}
    drops = [(rid, v, reason) for rid, (v, reason) in all_verdicts.items()
             if v == "drop" and rid in by_id]
    keeps = [rid for rid, (v, _) in all_verdicts.items() if v == "keep"]
    unclassified = [r["id"] for r in rows if r["id"] not in all_verdicts]

    print(f"\nkeep: {len(keeps)}  drop: {len(drops)}  unclassified(kept as-is): {len(unclassified)}")
    print("\nsample of drops:")
    for rid, v, reason in drops[:25]:
        r = by_id[rid]
        print(f"  [{rid}] {r['company']} | {r['level_type']} | price={r['price']} | "
             f"{reason} | {r['raw_text'][:60]}")

    drop_by_company: dict[str, int] = {}
    for rid, _, _ in drops:
        c = by_id[rid]["company"]
        drop_by_company[c] = drop_by_company.get(c, 0) + 1
    print("\ndrops by company (top 20):")
    for c, n in sorted(drop_by_company.items(), key=lambda x: -x[1])[:20]:
        print(f"  {c}: {n}")

    if not apply:
        print("\n(dry run — rerun with --apply to actually delete these rows)")
        con.close()
        return

    ids = [rid for rid, _, _ in drops]
    for j in range(0, len(ids), 500):
        chunk = ids[j:j + 500]
        qmarks = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM dossier_price_levels WHERE id IN ({qmarks})", chunk)
    con.commit()
    con.close()
    print(f"\ndeleted {len(ids)} rows.")


if __name__ == "__main__":
    main()
