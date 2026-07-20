# YouTube Recorder 设计文档 v0.2

> **YouTube Recorder** · By Leoluchino

> 取代 v0.1。依据 2026-07-18 审计报告重写，并纳入用户四项裁决。
> 产品定义：**面向 Obsidian 的 YouTube 订阅发现、转录编排、全文可读化和视觉证据采集器。**
> 不重做 MacWhisper 已成熟的转录本身；核心价值在 MacWhisper 没有解决的链路：自动发现 → 无字幕视频稳定排队转录 → 原始 transcript 永久保存 → 全文忠实改写 → 按语义截取视频画面 → 按 Vault 治理规则入库。

---

## 0. 已定决策（本版基线）

| 决策项 | 结论 |
|---|---|
| 轮询 | 每 2 小时（`StartCalendarInterval` 偶数小时数组 + `RunAtLoad`；**不用** `StartInterval`，它在睡眠时错过不补跑） |
| 弹窗 | RSS 扫描静默；**仅在发现新视频时**弹窗（"发现 N 条新视频"，30 秒可取消）；取消则视频保留在队列，下一轮再问 |
| Vault 治理 | **模式 A**：工具只允许在 `20-Raw/YouTube/` 新建不可变原料文件（不改旧文件）；AI 文章进 `30-Wiki/`；附件进 `40-Attachments/YouTube/{video_id}/` |
| 文章模式 | 默认 `edited_article`（重组结构、不新增事实、每段保留来源 segment 映射）；`faithful_cleanup`、`wiki_note` 作为可选模式保留 |
| v0.1 范围 | 可靠 worker + 全文改写 + 视觉截图 + 只读 Reports 阅读器 + **频道管理/设置 GUI（不后置）** |
| 转录主路径 | **无字幕是默认场景**。MacWhisper Watch Folder + SRT 导出为主适配器；OpenAI 音频 API 为备用适配器；字幕仅是机会型快路径 |
| 凭证 | macOS Keychain，不写 plist；日志脱敏 |
| 状态存储 | SQLite（标准库），不用可变 JSON |

---

## 1. 架构

```
launchd (偶数小时 + RunAtLoad)
   │
   ▼
Discovery(RSS, 静默) ──发现新视频──▶ 弹窗确认(30s) ──▶ SQLite Job Store
                                                        │
        ┌───────────────────────────────────────────────┘
        ▼
  Metadata Probe (yt-dlp) ──▶ 字幕可用? ──是──▶ 字幕标准化为 timestamped segments
        │否
        ▼
  音频下载 ──▶ Transcription Adapter ──▶ Transcript Validator ──▶ canonical JSON
        │        (MacWhisper SRT 主 / OpenAI API 备)         │
        │                                          ┌─────────┴─────────┐
        │                                          ▼                   ▼
        │                                  Article Transformer   Visual Cue Planner
        │                                  (分块全文改写)          │
        │                                          │          片段下载→多帧抽取→视觉复核
        │                                          ▼                   ▼
        │                                   Vault Package Builder ◀────┘
        │                                          ▼
        │                                   Atomic Writer → Read-back Verifier
        ▼
  GUI (localhost)：Channels / Queue / Reports(只读阅读器) / Settings
```

核心原则：时间码永不丢失；原料与派生分离；videoId 为唯一身份；每阶段可恢复；有预算边界。

---

## 2. 数据与状态

### 2.1 应用目录（不放 Vault 内）

```
~/Library/Application Support/YouTube Recorder/
├── config.yaml
├── state.sqlite3
├── logs/
├── work/{video_id}/
│   ├── metadata.json
│   ├── audio.m4a
│   ├── transcript.original.srt      # 永不覆盖
│   ├── transcript.canonical.json    # 带时间码 segments
│   ├── article.draft.json
│   ├── visual-plan.json
│   ├── frames/
│   └── manifest.json
├── macwhisper-inbox/   # Watch Folder
├── macwhisper-outbox/
└── dead-letter/
```

### 2.2 SQLite 表

`channels`（channel_id, url, enabled, not_before）、`videos`（video_id 唯一, status）、`jobs`（stage, attempt, run_after, lease_until）、`artifacts`（video_id+kind+version 唯一, path, sha256, verified_at）、`attempts`、`costs`、`visuals`、`writes`（vault_path, content_hash, readback_ok）。

所有状态转换在短事务内完成；单进程锁 + job lease 防并发。

### 2.3 状态机（简化）

```
discovered → metadata_ready → caption_check
  ├─ 有字幕 → transcript_ready
  └─ 无字幕 → audio_queued → awaiting_transcription → transcript_ready
transcript_ready → article_ready ─┐
transcript_ready → visual_planned → frames_ready ─┤
                                    package_ready → written → verified
任意阶段 → failed → (retry from safe stage | dead_letter)
```

重试分类：瞬时（退避重试）/ 资源（暂停队列提示人工）/ 永久（私有、删除、地区限制 → dead_letter）/ 数据（换适配器或人工复核）/ 写入（修复后从 package 重试）。

### 2.4 canonical transcript（时间码是一等数据）

```json
{
  "video_id": "abc123", "language": "zh", "duration_ms": 2537000,
  "segments": [
    {"segment_id": "s0042", "start_ms": 312400, "end_ms": 320900,
     "text": "……", "source": "macwhisper_srt", "confidence": null}
  ]
}
```

验收：≥95% 有声时长被覆盖；时间单调递增；纯文本只是派生视图。

---

## 3. 转录

### 3.1 字幕快路径（机会型，不是主路径）

yt-dlp 先列出字幕轨，按明确优先级选一条（人工 zh > 人工 en > 自动 zh > 自动 en），保存实际 language tag；VTT/SRT 解析为 canonical segments，**保留时间码**。字幕不可用只记 `caption_unavailable`，不算错误。

### 3.2 主适配器：MacWhisper Watch Folder + SRT

1. 音频以 `{video_id}.m4a` 命名，原子移动进 inbox。
2. MacWhisper（已装 14.2，需确认 Pro/Watch Folder 可用）导出 `{video_id}.srt` 到 outbox。
3. 文件稳定性检测（大小不变 N 秒）→ SRT 校验（语法、时长覆盖、空段、乱码）→ canonical JSON。
4. 超时（默认 180 分钟）或校验失败 → 转备用适配器，不重复建笔记。

Watch Folder 官方仍标 Beta，因此自有 job 状态、超时和 fallback 是必需品，不是锦上添花。

### 3.3 备用适配器：OpenAI 音频 API

- 模型放配置（`whisper-1` 支持 segment/word 时间码；`gpt-4o-transcribe` 系列按文档核对时间码能力后才可用于截图路径——**时间码能力是适配器契约**）。
- 25 MB 上限处理：① ffmpeg 压 16 kHz 单声道低码率（32 kbps 下 ≈100 分钟/25 MB）；② 仍超限按时长切块，**切点前后各留 15 秒重叠**；③ 各块转录后优先用时间码对齐合并重叠区，LLM 仅在文本歧义时参与拼接裁决。
- 每块时间码加块偏移量还原为全局时间。

### 3.4 whisper.cpp

保留为第三适配器（代码内可选），v0.1 不作为验收路径。

---

## 4. 全文可读化（Article Transformer）

**禁止截头去尾。** 分层全文处理：

1. 按时间码/主题切成 5–12 分钟语义块。
2. 每块提取忠实摘要、实体、数字、论点、视觉提示（便宜模型）。
3. 全部块摘要 → 全局大纲。
4. 按大纲逐章节改写（`edited_article`），每段只允许引用指定 segments，保留 `source_segment_ids`。
5. 全局校对：术语、人名、数字、重复段。
6. 反向溯源检查：无来源句删除或标"待查"。
7. 生成 title_zh、aliases、one_sentence、summary、takeaways、tags。

模型输出 Schema 约束的 JSON，由确定性 renderer 生成 Markdown——不让 LLM 直接产最终文件。不可变原则：原始 transcript 永不覆盖；AI 不补充 transcript 外的事实；不确定的专名数字保留原貌标记待查；外语内容保留关键原文附中文翻译。

成本控制：按 token 估算分块；单视频 token/费用/重试上限；块级 hash 缓存，改 prompt 只重跑受影响阶段。

---

## 5. 视觉证据（截图管线）

召回宁多（规则扫描显式提示"看图/看表/看屏幕/as you can see"+ 隐式指代"第二列/红线/这个按钮"），精排宁少（LLM 判断是否必须看画面 + 视觉模型/OCR 判断帧是否有信息）。

流程：candidates（segment_ids, target_ms, window_ms, expected_visual, confidence, insert_after_section）→ yt-dlp `--download-sections` 按需下载低清片段 → `t-2/t/t+2` 多帧抽取（时间窗默认 start−3s ~ end+5s）→ 清晰度/黑屏/OCR 文本量/相关性评分选帧 → pHash 去重 → 压缩保存。

文件：`40-Attachments/YouTube/{video_id}/{video_id}--{HHMMSS}--{slug}.jpg`；图旁必须有时间点、一句理由、`?t=` 原视频链接；实际选中帧时间写回 manifest。

**图片密度拖杆 1–5**（GUI 组件）：同时调节候选阈值、段落覆盖目标、最小间距和 soft_max（1 档 ≈0–3 张 … 5 档逐段尝试）。档位 5 的"每段一张"是强目标不是硬命令：窗口内只有 talking-head/黑屏/重复幻灯片时显示"本段未找到相关画面"并跳过；`strict_fill` 默认 false。UI 实时预估图片数和额外成本。

验收（30 个人工标注候选点）：召回 ≥90%，插图精确率 ≥80%，选帧可读相关 ≥85%，近重复 ≤5%。

---

## 6. Vault 入库（治理模式 A）

双产物：

- **原料**（不可变）：`20-Raw/YouTube/{发布日期} {原始标题}--{video_id}.md` —— 原始 transcript + 元数据 + manifest 引用；工具只新建、永不修改已有文件。
- **知识**：`30-Wiki/` 下按 `10-Schema/video.md` 渲染（`type: video`、aliases、created/updated、嵌套 sources、status、规定段落），正文为 AI 文章 + 插图 + 时间点链接 + 指回原料和 YouTube URL。

写入规则：文件名含稳定 videoId；重跑按 frontmatter `youtube_video_id` 检索更新而非新建；同目录临时文件 + 原子 rename；写后 read-back 校验 frontmatter、图片链接、manifest；路径规范化，拒绝 `..`/绝对子路径/symlink 逃逸。

---

## 7. GUI（v0.1 全量，localhost 单应用）

四个页签：**Channels / Queue / Reports / Settings**。

品牌规范：产品名一律写 **YouTube Recorder**；所有内部展示位（GUI 页脚、关于页、弹窗标题、生成笔记 frontmatter 的 `generator` 字段、CLI `--version`、日志头）统一署名 **By Leoluchino**。弹窗标题格式：`YouTube Recorder · By Leoluchino`。launchd label：`com.leoluchino.youtube-recorder`。

- **Channels**：粘贴任意频道 URL → 解析稳定 channel ID；启用/停用、备注、`not_before`（默认订阅加入日，不回填历史）。
- **Queue**：各视频阶段状态、失败原因、重试按钮、dead-letter 查看。
- **Reports（只读阅读器）**：报告列表（标题/频道/日期/状态/是否含图）+ 搜索过滤；Markdown/GFM/表格/代码块/本地图片渲染；frontmatter 折叠卡；自动目录；`[[wikilink]]` 解析（多匹配时列出不猜）、`![[embeds]]` 图片；"在 Obsidian 打开/Finder 显示"；文件更新自动刷新；默认只读，编辑归 Obsidian。
- **Settings**：转录适配器选择（MacWhisper / OpenAI API / whisper.cpp）、文章模式、图片密度拖杆（含预估）、路径、预算、保留策略、弹窗开关。

安全：只绑 `127.0.0.1`；每次启动生成 CSRF token；文件读取白名单限 Vault 根 + 报告目录，canonical path 检查；Raw HTML/script 严格净化；频道 URL 走参数化 subprocess；配置写入走 schema 校验 + 临时文件原子替换；GUI 与 worker 经 SQLite 协调，不同时改 YAML。

---

## 8. 调度、凭证、可观测性

- launchd：`StartCalendarInterval` 偶数小时数组 + `RunAtLoad`；错过的多次触发唤醒后合并为一次；RSS 窗口本身提供补抓能力。
- 弹窗：发现新视频后 osascript 弹"发现 N 条新视频，30 秒后开始处理"[取消/立即处理]；超时=继续；取消=视频留队列下轮再问；锁屏/无 GUI 会话时按 `on_dialog_error: run|skip` 配置（默认 run）。
- 凭证：Keychain 按服务名读取；只配一个 provider 也能跑（未配置的 stage 禁用）；日志不打印 key/header/完整 webhook。
- 结构化日志：run_id, video_id, stage, attempt, event, elapsed_ms, result, error_code, provider, model。每轮汇总：发现/排队/完成/失败/dead-letter 数、各阶段耗时、下载 MB、转录分钟、tokens、估算费用、无字幕比例、截图采纳数、read-back 成功率。
- CLI（与 GUI 并存）：`run --once / status / inspect VIDEO_ID / retry VIDEO_ID --from STAGE / channels add|list`。
- 保留策略：音频 7 天、视频片段 2 天、原始 transcript 永久、失败工作目录 30 天。
- 合规：默认仅个人私有 Vault 使用；不自动公开发布 transcript/截图；不绕过年龄/付费/地区限制；频道可设 `permission_basis`。

---

## 9. 工程底座

- Python 包结构（非单文件 main.py）；`pyproject.toml` + 虚拟环境 + 锁定依赖（本机 Python 3.14，项目固定支持版本）。
- 配置用 Pydantic/JSON Schema 校验；LLM 输出全部过 Schema 验证。
- 依赖：yt-dlp、ffmpeg、openai、anthropic、flask、pydantic；whisper.cpp 可选。

---

## 10. 测试与验收（v0.1 门槛）

样本集：≥20 条真实无字幕视频为主（含中英混合、>90 分钟、图表密集、屏幕演示、纯 talking-head、私有/删除/年龄限制各若干）。

| 指标 | 门槛 |
|---|---:|
| 无字幕样本成功产出 transcript | ≥90% |
| 同 videoId 重跑产生重复笔记 | 0 |
| 时间码合法率 | 100% |
| 文章段落 source 覆盖率 | ≥95% |
| 人工抽查无来源事实（阻断级） | 0 |
| 视觉召回/插图精确率/选帧质量 | ≥90% / ≥80% / ≥85% |
| Vault read-back | 100% |
| 崩溃恢复丢 job | 0 |
| 日志泄密 | 0 |

---

## 11. 路线图（GUI 不后置版）

| 阶段 | 内容 | 量级 |
|---|---|---|
| P0 决策+样本 | 剩余确认项（见 §12）+ 20–30 条真实样本 manifest | 1–2 天 |
| P1 Spike | MacWhisper Watch Folder→SRT 链路、`mw` CLI 时间码能力、3 条无字幕视频时间偏移实测 | 1–3 天 |
| P2 骨架 | 包结构、SQLite、状态机、日志、锁、CLI | 2–4 天 |
| P3 发现+下载 | 频道解析、RSS、not_before、metadata probe、音频下载 | 3–5 天 |
| P4 转录+验证 | Watch Folder adapter、SRT parser、QA、OpenAI 备用适配器（压缩/切块/15s 重叠） | 3–6 天 |
| P5 全文改写 | 分块→大纲→逐章→溯源检查→renderer | 4–7 天 |
| P6 视觉证据 | 召回→片段下载→抽帧→精排→去重→插图 | 5–8 天 |
| P7 Vault 入库 | 模式 A、Schema renderer、原子写、read-back | 3–5 天 |
| P8 调度 | launchd、Keychain、弹窗、72h 试运行 | 2–4 天 |
| P9 GUI | Channels/Queue/Reports/Settings 四页 + 密度拖杆 | 5–9 天 |
| P10 Beta | 真实频道 canary、忠实度与截图人工核对 | 5–10 天 |

总量级约 6–8 周（单人）。P1 spike 结论若否定 Watch Folder 可行性，主适配器切 OpenAI API，MacWhisper 降级备选——架构不变，只换适配器优先级。

---

## 12. 开工前剩余确认项

1. MacWhisper 是否 Pro 版、Watch Folder 与自定义 SRT 导出是否可用？（P1 spike 第一件事验证）
2. Vault 根目录确切路径，以及 `20-Raw/YouTube/`、`40-Attachments/YouTube/` 的例外条款是否需要同步写进 `AGENTS.md`（模式 A 的正式化）。
3. 频道清单（GUI 做好前，P3 测试需要 2–3 个真实频道先行）。
4. 云端转录的费用上限（单视频/单月），用于熔断阈值。
