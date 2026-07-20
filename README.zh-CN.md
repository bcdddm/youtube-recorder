# YouTube Recorder · By Leoluchino

[English](README.md) | 中文

YouTube 订阅 → 自动转录 → AI 整理成文（含智能截图）→ 写入 Obsidian。本地优先的 macOS 菜单栏应用。当前版本 **v0.4.2**。

## 它做什么

订阅 YouTube 频道（或粘贴单个视频链接）。按你设定的排班表自动发现新视频：有字幕直接抓取，无字幕转录音频；把完整口述文稿改写成可读的中文文章（忠实、可溯源到时间码、不丢中段），按内容语义截取画面插入对应章节，最后写入 Obsidian 库或独立文件夹——完整原文以可折叠块附在文末。

## v0.4 亮点

- **原文保留档位**：0/40/50/60/70/80/90/100%——正文中至少该比例的字符**逐字来自原文**（程序保证：AI 只选句和写过渡，被选句子由程序原样拷贝，不足自动补齐，实测值写入 frontmatter）。70% 以下允许 AI 重排句序；无意义句可剔除；保留句只许"修正"（改动少于 3 个词）不许改写。
- **AI 重标点**：转录稿口语流重新断句加标点，剥标点逐字校验保证内容零改动，校验失败自动回退机械补标。另有确定性语气词清理与错别字校对轮。
- **按事件分段**：每个事件一节、每节不超过 600 字（2 分钟内读完），超长自动拆分。
- **智能截图密度 1–5**：规则召回 + LLM 推断（叙述式视频也能配图）；**5 级程序保证每个自然段至少一张配图**（无命中画面时按该段时间点自动截取，写入时逐节分配）。
- **Reports 报告库**：横向时间轴默认视图、组胶囊多选筛选、标签云（实测行高折叠两行、悬停展开三倍再滚动）、**AI 归并同义标签**（如"AI 技术/AI 投资→AI"，展示层映射、原始数据不动）、当日情报汇总（全要点覆盖+【标题】溯源，30 天缓存同条件秒开）、文章内"问 AI"与一键重新总结、回收站 3 天可恢复。
- **分环节 AI 路由**：整理/截图/问答可分别指定 OpenAI 或 Anthropic，并选择具体模型（用你的 key 实时拉取各家最新模型列表）。
- **中英双语界面**：设置第 ⓪ 节选语言，选择即全局生效；语言区块永久双语。
- **按 Release 版本更新**：软件内"检查更新"只跟随已发布的 GitHub Release 标签，main 上未发布的提交不会推送给用户。
- **频道多组管理**：一个频道可属多个组，按组筛选报告与汇总范围；队列中已完成条目标题直达阅读页。

## 自动运行

launchd 常驻（`com.leoluchino.youtube-recorder`）：按排班表整点运行，睡眠错过的唤醒后合并补跑；发现新视频时弹窗 30 秒可取消。菜单栏托盘常驻，关窗不退出。

## 常用命令

```bash
cd "~/Coding/YouTube Recorder/app"
python3 -m youtube_recorder.cli status              # 各阶段数量
python3 -m youtube_recorder.cli run --once          # 手动跑一轮
python3 -m youtube_recorder.cli channels add <URL>  # 加频道
python3 -m youtube_recorder.cli inspect <VIDEO_ID>  # 单视频全记录
python3 -m youtube_recorder.cli retry <VIDEO_ID> --from <STAGE>
python3 -m youtube_recorder.cli tray                # 托盘 + GUI（127.0.0.1:8765）
./scripts/build_app.sh                              # 打包双击可开的 .app
```

## 关键路径

| 什么 | 在哪 |
|---|---|
| 代码 | `~/Coding/YouTube Recorder/app/` |
| 配置 | `~/Library/Application Support/YouTube Recorder/config.yaml` |
| 数据库/日志/工作目录 | 同上目录下 `state.sqlite3` / `logs/` / `work/` |
| MacWhisper 投放箱 | `~/Coding/YouTube Recorder/macwhisper-inbox/` |
| API key | macOS 钥匙串（`ytrec-openai` / `ytrec-anthropic`），不进配置文件 |
| 产出 | Obsidian Vault `20-Raw/YouTube/`（原料，不可变）+ `30-Wiki/`（文章）+ `40-Attachments/YouTube/`（截图）；或独立文件夹（分层/纯平铺） |

## 可靠性与安全

SQLite 状态机（按视频 ID 幂等）、原子写入 + 读回校验、结构化 JSONL 日志（密钥自动脱敏）、分类重试策略。API key 只存钥匙串；GUI 仅监听 127.0.0.1，全 POST 校验 CSRF，vault 路径逃逸拦截。个人私用工具，请遵守 YouTube 服务条款与版权法。

## 下一步（backlog）

视觉模型复核截图相关性（vision QA）、`[[wikilink]]` 完整解析、OpenAI 音频切块路径实跑验证、P10 canary 72h 报告。

## 测试

```bash
cd app && for t in tests/test_*.py; do python3 $t; done
```
