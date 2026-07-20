# YouTube Recorder · By Leoluchino

[English](README.md) | 中文

YouTube 订阅 → 自动转录 → AI 整理成文（含智能截图）→ 写入 Obsidian。v0.3.0

## v0.3.0 亮点（2026-07-20）

菜单栏托盘常驻（关窗不退出）、原生窗口 App、深浅双主题（Notion 风）+ Haring 风手绘插图、
两段式运行模式（自动/确认，均可随时跳过单条）、单视频链接直接处理、文章库搜索/分组/回收站（3 天可恢复）、
报告内"问 AI"细节查询与一键重新总结、Reports 保存位置可改+一键迁移、图片自动重试+手动刷新、
LLM 智能截图召回（叙述式财经视频也能配图）、守候式转录回收（出稿即成文不等下一轮）、
arm64 架构修复、首次使用强制引导配置 API 密钥。

## 当前状态（2026-07-19）

**P0–P9 全部完成并在真实数据上验证。**

- P6 视觉截图：规则召回（中英显式/隐式提示词）→ 低清片段下载 → t±2s 三帧抽取 → 清晰度选帧 → aHash 去重 → 按密度 1–5 插入文章对应章节；talking-head 视频自动不配图（宁缺毋滥）。视觉模型复核（vision QA）留作 v0.3。
- P9 GUI：`python3 -m youtube_recorder.cli gui` → http://127.0.0.1:8765 。四页：Channels（加频道/启停）、Queue（状态总览/重试）、Reports（文章列表 + Markdown 阅读器，含 vault 图片安全路由）、Settings（**24 小时排班表**、弹窗策略、转录方式、文章模式、截图密度拖杆、vault 路径、API key 钥匙串管理）。保存排班表自动重写 plist 并 reload launchd。安全：仅监听 127.0.0.1，全 POST 校验 CSRF，vault 路径逃逸拦截。

已跑通的完整闭环：RSS 发现新视频 → 无字幕检测 → 音频下载 → MacWhisper Watch Folder 转录 → SRT 校验（幻觉裁剪/越界裁剪）→ 分块全文 AI 改写（不丢中段，带溯源）→ Obsidian 双产物写入（`20-Raw/YouTube` 不可变原料 + `30-Wiki` 文章，read-back 校验）。

首批两篇成品：`30-Wiki/美股市场动态与AI新模型影响--pvq2_MY8VFY.md`、`30-Wiki/台积电、谷歌与奈飞的市场动态--KueAYEGSolI.md`。

## 自动运行

launchd 已启用（`com.leoluchino.youtube-recorder`）：每偶数小时整点运行，登录时补跑；发现新视频时弹窗 30 秒可取消，取消则顺延下一轮。

## 常用命令

```bash
cd "~/Coding/YouTube Recorder/app"
python3 -m youtube_recorder.cli status              # 各阶段数量
python3 -m youtube_recorder.cli run --once          # 手动跑一轮
python3 -m youtube_recorder.cli channels add <URL>  # 加频道
python3 -m youtube_recorder.cli channels list
python3 -m youtube_recorder.cli inspect <VIDEO_ID>  # 单视频全记录
python3 -m youtube_recorder.cli retry <VIDEO_ID> --from <STAGE>
```

## 关键路径

| 什么 | 在哪 |
|---|---|
| 代码 | `~/Coding/YouTube Recorder/app/` |
| 配置 | `~/Library/Application Support/YouTube Recorder/config.yaml` |
| 数据库/日志/工作目录 | 同上目录下 `state.sqlite3` / `logs/` / `work/` |
| MacWhisper 投放箱 | `~/Coding/YouTube Recorder/macwhisper-inbox/` |
| API key | macOS 钥匙串（`ytrec-openai` / `ytrec-anthropic`），不进配置文件 |
| 产出 | Obsidian Vault `20-Raw/YouTube/`（原料）+ `30-Wiki/`（文章） |

## 下一步（v0.3 backlog）

- 视觉模型复核截图相关性（vision QA）、OCR 评分、`[[wikilink]]` 完整解析、OpenAI 音频 API 备用适配器的切块实跑验证、P10 canary（连续 72h 无人值守观察）。
- 建议：把 20-Raw/YouTube 例外条款正式写进 vault 的 AGENTS.md；OpenAI key 曾在对话中明文出现过，建议 rotate。

## 测试

```bash
cd app && for t in tests/test_*.py; do python3 $t; done
```
