"""一次性执行：2026-08-05 用户确认过的公司档案清理方案。

读 /tmp/dossier_classify_parsed.json（AI 对当时 106 个条目的分类），套用
用户在 AskUserQuestion 里确认过的两个修正：
  1. SOXL / SOXX / SPCX / 纳斯达克100 这类 ETF/指数不跟着"非实体"一起
     归档，单独留在列表里、归为 index_etf 类。
  2. "沃尔什"这种真实人物不该被判 drop（AI 自己对其他人物如鲍威尔/巴菲特
     都判了 keep，这条不一致），改成保留。
另外修正一个 AI 自己的小错误：Azure/Copilot/Microsoft 365 的合并目标它
写成了列表里根本不存在的"Microsoft"，改成实际存在的"微软"。

非公司/实体的条目不会被删除——移进 50-公司档案/_归档/ 子文件夹，内容原样
保留，真想删可以自己在 Finder/Obsidian 里删。别名条目的内容合并进对应的
canonical 笔记后，原文件也移进同一个归档文件夹。
"""

import json
from pathlib import Path

from youtube_recorder import config as cfg_mod
from youtube_recorder import db as dbm
from youtube_recorder import dossier

INDEX_ETF_OVERRIDE = {"SOXL", "SOXX", "SPCX", "纳斯达克100"}
KEEP_OVERRIDE = {"沃尔什"}
MERGE_TARGET_FIX = {"Microsoft": "微软"}


def main() -> None:
    cfg = cfg_mod.load()
    root = cfg.vault_root
    assert root, "vault.root 没配置"
    con = dbm.connect()

    data = json.loads(Path("/tmp/dossier_classify_parsed.json").read_text(encoding="utf-8"))
    by_name = {d["name"]: d for d in data}

    archived = []
    merged = []
    kept_entity = []
    kept_index_etf = []
    skipped = []

    for item in data:
        name = item["name"]
        verdict = item["verdict"]

        if name in INDEX_ETF_OVERRIDE:
            dbm.dossier_register_entity(con, name, category="index_etf", status="approved")
            dbm.dossier_set_entity_status(con, name, "approved")
            kept_index_etf.append(name)
            continue

        if name in KEEP_OVERRIDE:
            dbm.dossier_register_entity(con, name, category="entity", status="approved")
            dbm.dossier_set_entity_status(con, name, "approved")
            kept_entity.append(name)
            continue

        if verdict == "keep":
            dbm.dossier_register_entity(con, name, category="entity", status="approved")
            dbm.dossier_set_entity_status(con, name, "approved")
            kept_entity.append(name)

        elif verdict == "drop":
            ok = dossier.archive_entity_note(root, name)
            dbm.dossier_register_entity(con, name, category="entity", status="rejected")
            dbm.dossier_set_entity_status(con, name, "rejected")
            archived.append((name, ok))

        elif verdict == "merge":
            target = item.get("merge_into") or ""
            target = MERGE_TARGET_FIX.get(target, target)
            if not target:
                skipped.append(name)
                continue
            # 确保 canonical 目标本身已登记为 approved 实体
            dbm.dossier_register_entity(con, target, category="entity", status="approved")
            dbm.dossier_set_entity_status(con, target, "approved")
            ok = dossier.merge_entity_note(root, name, target)
            dbm.dossier_set_entity_alias(con, name, target)
            # 把这个别名下过去记的 dossier_processed 行转记到 canonical 名下，
            # 避免以后同一篇文章又被重复当成"新公司"处理一遍
            con.execute(
                "UPDATE OR IGNORE dossier_processed SET company=? WHERE company=?",
                (target, name))
            con.execute("DELETE FROM dossier_processed WHERE company=?", (name,))
            con.commit()
            merged.append((name, target, ok))
        else:
            skipped.append(name)

    con.close()

    print(f"kept as company/entity: {len(kept_entity)}")
    print(f"kept as index/etf: {len(kept_index_etf)} -> {kept_index_etf}")
    print(f"archived (not a real entity): {len(archived)}")
    for n, ok in archived:
        print(f"  archive {'OK' if ok else 'MISSING'}: {n}")
    print(f"merged: {len(merged)}")
    for src, tgt, ok in merged:
        print(f"  merge {'OK' if ok else 'MISSING'}: {src} -> {tgt}")
    if skipped:
        print(f"skipped (no verdict/target): {skipped}")


if __name__ == "__main__":
    main()
