# 数据来源与扩展

脚本只解析以下位置的会话，不扫描配置文件中的凭据。所有默认路径相对于运行用户的 home；`--home` 可使用测试或备份目录，`--root 名称=/绝对路径` 可覆盖任一来源。安装位置是通用 `~/.agents/skills/daily-recap`，Pi 支持自动发现；其他宿主是否自动加载取决于其 skills 配置。

| 来源名称 | 默认路径 | 解析方式 |
|---|---|---|
| pi | `.pi/agent/sessions` | JSONL message，session 提供 cwd/id |
| claude | `.claude/projects` | JSONL user/assistant message，含子代理 |
| codex | `.codex/sessions` | session_meta + response_item/message；不重复读 event_msg 镜像 |
| gemini | `.gemini/tmp` | `session-*.json` messages；本机未找到实际会话，仅使用格式测试 |
| cursor | `.cursor/projects` | agent-transcripts 导出；对应数据库会话时由数据库取代，未对应的无时间导出仍报告缺口 |
| cursor-db | `.config/Cursor/User/globalStorage/state.vscdb` | cursorDiskKV bubbleId 的 createdAt/text/type；composerHeaders + workspace.json 关联项目 |
| opencode | `.local/share/opencode/opencode.db` | SQLite session/message/part，只读连接 |
| zcode | `.zcode/cli/db/db.sqlite` | 与 OpenCode 相同的文本 part 关系表 |
| zcode-desktop | `.zcode/v2/sessions` | session_*.json 的 meta 与 messages |
| kimi | `.kimi/sessions` | wire.jsonl TurnBegin / ContentPart，Unix 秒时间 |
| kimi-code | `.kimi-code/sessions` | wire.jsonl turn.prompt / content.part，毫秒时间；包括子代理 |
| deepseek-harness | `.dsh/sessions` | session*.jsonl 或 .jsonl.zstd；user/message、assistant/message、turn/end 错误 |

## 本机验证范围

初次实测扫描到 Pi、Claude、Codex、Cursor、OpenCode、Zcode 两种存储、Kimi 两代 wire 与 DeepSeek Harness 压缩会话。后续已接入 Cursor 带消息时间的数据库，59 份导出会话 ID 均在数据库中对应，以数据库为主来源。覆盖表区分历史可解析但当日无活动、无会话文件、缺失、部分覆盖；记录历史消息总数及起止时间，不能把零命中当解析成功或失败的唯一证据。

额外目录逐项核对：`.trae` 仅 skills；`.trae-cn` 是编辑器资源，会话库转到 `.config/Trae CN/ModularData/ai-agent/database.db`；Copilot 和 Crush 标准会话存储缺失；Trae 原生库已加密（应用日志有确认），仍是未闭环项。客户端可读状态缓存无完整消息，不能替代数据库。Gemini 未找到会话。DeepSeek TUI 与 DeepSeek Harness 为不同产品，本适配器针对后者。

## 低 token 设计

1. Python 读取全部已知会话流，按消息时间筛选，不把原始历史交给模型。
2. 只提取 user/assistant 自然语言；默认不保留 thinking、工具参数与工具输出。工具验证需要时定点回查。
3. 精确重复消息去重；简报按 cwd 排序；模型语义合并父子会话及不同 agent 的同项工作。
4. 每条证据默认保留首尾最多 6000 字符，中间截断明确标记。`--max-chars 0` 可完整采集，通常不需要。
5. 简报默认最多约 12000 字符，自动缩短每会话摘录。超多会话时明确标示未展示的数量与分页入口，不静默省略。
6. 关键词只从最后一条 assistant 回复提取待办候选，避免把早期已修复错误重新当成断点。仍可能出现误报或漏报，必须由模型结合末次要求判断。
7. 复用已生成的 index/sessions，不为调整简报重复扫描。字符数不是精确 token 数；中文比例、模型分词影响实际消耗。

## 数据边界

- Cursor 优先使用数据库消息自身的 createdAt，项目由 composerHeaders 与 workspace.json 关联。导出镜像按会话 ID 对应后不重复计数；不是给每条导出消息推算时间。数据库缺失/不可读/未对应的导出仍如实标记，无论如何不拿 mtime 当消息日期。
- Kimi 旧版只有 context.jsonl 没有 wire 的会话无法可靠归日；本采集不从无时间 context 补猜。
- 只解析自然语言消息；只有工具事件而没有文字的会话可能不入选。附件、图片、压缩前被清掉的历史、远端未同步会话不在保证范围内。
- 记录源可能含分支、fork 或子代理，记录单元数不是独立任务数。脚本保留原始分支证据，模型不能把相互矛盾的分支揉成一个成果。
- 目标日期后的更新包括原会话后续和相同 cwd 的新会话候选。新会话是否解决旧断点由模型判断，不能仅凭同目录断言；跨目录或缺失 cwd 的关联不保证。
- 凭据脱敏是常见模式匹配，不保证识别任意自然语言泄露。模型输出前仍须检查。
- `coverage.md` 是简洁的状态表与逐项结论；完整错误、无时间源和候选调查证据放 `coverage.json`。未解决项单列，资源目录与无会话来源不混作采集故障；没有解析错误不等于本机全部客户端已覆盖。

## 添加来源

1. 只查路径和 schema，先确认真实存储位置、消息时间单位、角色、正文、session ID、cwd、子会话关系；不要从凭据文件找线索。
2. 在 `collect.py` 的 ROOTS 注册名称与默认路径；在 `adapters.py` 写适配器或复用现有 SQLite 适配。
3. 统一产出 `(session_id, cwd, role, aware_datetime, text, source_reference)`；文本流要保留行号，JSON 保留数组索引，数据库保留主键。
4. 在 collect 的路径选择/分发中接入。真实时间无法获取时返回 None，以覆盖缺口报告，不自行推测。
5. 用合成记录验证日期边界、缺失字段、损坏记录、重复来源，以及只读行为，然后在真实本机样本上运行。
6. 修改本来源表。标注“已实现但只有测试样本”和“已用真实数据验证”的差别。

验证命令：

```bash
python3 -m unittest discover -s /绝对路径/daily-recap/tests -v
```

## 固定报告库与周期汇总

默认报告库为 `~/.local/share/daily-recap`，永远不依赖当前工作目录：

```text
~/.local/share/daily-recap/
├── daily/2026-10-27.md
├── weekly/2026-W44.md
├── monthly/2026-10.md
├── yearly/2026.md
└── evidence/<daily|weekly|monthly|yearly>/<周期>/<运行时间>/
```

`run.py` 默认准备昨天日报，每次先检查来源文件的大小、mtime/ctime、SQLite WAL 和解析器版本指纹。完全未变时直接返回 `cached`；来源有变则脚本重新采集，按相关会话内容比较，只有相关证据变化才返回 `prepared` 交给模型更新。无关会话有变化不触发模型重写。未完成草稿也必须通过来源指纹核对才能复用。`--refresh` 可以跳过复用强制重做。

报告区分目标日期与证据截止时间；每次返回 `checked_at`，缓存元数据记录 `last_checked_at`。成稿的“当前断点”和“下一步建议”以最新相关证据更新，不能把早上的待办当成下午仍未完成。模型填写 draft 后调用 `reports.py publish manifest.json` 原子发布；自动更新也保存旧版，主目录仍只有同日一个固定报告文件。

`--period week|month|year --date YYYY-MM-DD` 选择该日所在周期。ISO 周从周一开始。周/月只用日报；年优先月报，缺少月报的月份使用日报，避免重叠计数。缺报、下级报告覆盖不足及周期未结束必须说明，不自动补编。上级报告每次检查下级报告内容指纹；来源变化就准备更新，未变则复用。年报只保证纳入当前已保存的月报版本，若月报自身尚未更新，需先更新相应月报。

`--store /固定绝对路径` 仅供明确迁移报告库或测试；日常不要按 cwd 切换 store。整个链路的 Python 脚本不调用模型、不发网络请求，只有最后语义整理使用宿主 agent 的模型。

## 历史快照批量补报

```bash
python3 /绝对路径/daily-recap/scripts/backfill.py --year 2026
python3 /绝对路径/daily-recap/scripts/backfill.py --year 2026 --start 2026-04-01 --end 2026-04-30
```

历史模式单次扫描所有支持来源，再按带时区的消息日期拆分，自动填充每个已过去的日期。无消息日写空白占位，明确“未找到当前可读的记录，不代表没工作”。不写计划章节，不使用次日及更晚记录；只做确定性摘录，不调用模型。每个会话保存当天证据与来源位置，不仅生成空模板。

固定文件仍为 `daily/YYYY-MM-DD.md`，元数据 `mode=history, frozen=true`。再次运行时只检查哪些日期已经有历史快照；全部存在则退出，不读取原始会话、不比较来源指纹、不改报告时间。需要重建时显式 `--rebuild`。已有实时接续版会先备份再转换为历史版，转换后要恢复接续行为可明确使用 `run.py --mode live`。

历史模板：工作进展、断点与未闭环、会话索引、覆盖与局限。没有“下一步建议/计划”。索引为 `history-YYYY.md`，批次统计、覆盖报告、逐日证据和被替换的旧版放 `evidence/backfill/YYYY/<运行时间>/`。未结束的今天和未来日期不生成占位。

`run.py --mode auto` 是默认入口：昨天按实时接续，更早的日报按历史快照。显式 `--mode history --date YYYY-MM-DD` 读取历史快照，已有报告不再检查变化。周/月/年读取这些固定日报，不回扫原始会话；机器摘录的证据强度与空白限制必须在汇总中保留。

## 日常命令

```bash
# 检查来源后复用日报；相关证据变化时自动准备更新
python3 /绝对路径/daily-recap/scripts/run.py

# 重做更紧凑的简报，不重新扫描会话
python3 /绝对路径/daily-recap/scripts/brief.py /输出目录/index.json --budget 8000

# 查询某一会话末尾或包含特定词的消息
python3 /绝对路径/daily-recap/scripts/brief.py /输出目录/index.json --session S001 --tail 4 --budget 4000
python3 /绝对路径/daily-recap/scripts/brief.py /输出目录/index.json --session S001 --query 阻塞 --tail 3
```
