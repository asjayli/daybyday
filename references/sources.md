# 来源、接口与运行限制

技能主流程见 [SKILL.md](../SKILL.md)，安装见 [README.md](../README.md)。所有源路径相对运行用户 home；`--home` 可指向备份或测试目录，`--root AGENT=/绝对路径` 覆盖单一来源。

## 支持的来源

| 名称 | 默认路径 | 提取方式 |
|---|---|---|
| pi | `.pi/agent/sessions` | JSONL session/message |
| claude | `.claude/projects` | JSONL user/assistant，含子代理 |
| codex | `.codex/sessions` | session_meta + response_item/message，不重复读 event_msg |
| gemini | `.gemini/tmp` | session-*.json messages；目前仅格式 fixture 验证 |
| cursor | `.cursor/projects` | agent-transcripts；有对应带时间数据库记录时以数据库为主 |
| cursor-db | `.config/Cursor/User/globalStorage/state.vscdb` | cursorDiskKV bubble；workspace 元数据关联项目 |
| opencode | `.local/share/opencode/opencode.db` | SQLite session/message/part |
| zcode | `.zcode/cli/db/db.sqlite` | 同上 |
| zcode-desktop | `.zcode/v2/sessions` | session_*.json meta/messages |
| kimi | `.kimi/sessions` | wire.jsonl TurnBegin / ContentPart，Unix 秒 |
| kimi-code | `.kimi-code/sessions` | wire.jsonl turn.prompt / content.part，毫秒时间 |
| deepseek-harness | `.dsh/sessions` | session*.jsonl[.zstd]；不等于 DeepSeek TUI |

除 Gemini 外这些适配曾使用本地真实记录验证，但客户端版本变化仍需复核 schema。单元测试使用合成记录；“支持”不等于覆盖本机所有客户端、分支和附件。

Cursor 镜像按 session ID 对应；不能用导出文件 mtime 猜消息时间。Kimi 缺少带时间 wire 的 context 不强行归日。Trae 等额外来源只做目录/存储类型调查，未适配和加密来源列为缺口，不从凭据文件寻找线索。

数据库使用 `mode=ro`，正文仅取 user/assistant 文本。工具参数、工具输出、thinking、附件、图片、远端未同步或已清除历史不在默认提取范围。SQLite 原文件只读不代表系统不会管理 WAL/SHM；测试验证的是合成数据库主文件未变。

## CLI

`run.py` 等价于 `reports.py prepare`：

```bash
python3 <skill-dir>/scripts/run.py --date 2026-10-27 --timezone Asia/Shanghai
python3 <skill-dir>/scripts/run.py --period week --date 2026-10-27
python3 <skill-dir>/scripts/run.py --period month --date 2026-10-01
python3 <skill-dir>/scripts/run.py --period year --date 2026-01-01
python3 <skill-dir>/scripts/run.py --mode live --date 2026-10-20
python3 <skill-dir>/scripts/backfill.py --year 2026 --start 2026-04-01 --end 2026-04-30
```

- `--store` 仅用于明确迁移或测试；默认优先沿用旧 daily-recap 数据库目录，否则使用 daybyday。
- `--budget` 是字符数，不是模型 token 数；最小 2000，默认 12000。主简报、分页和定点读取都限额。
- 历史 `--rebuild` / run `--refresh` 表示用户明确重做；已存在成稿会备份。
- `collect.py --max-chars 0` 可完整保留单条证据；默认首尾共 6000 字符，历史批量首尾共 3000 字符，中段省略有标记。
- `collect.py --cache-dir PATH` 启用文件级解析缓存；run/backfill 自动使用报告库的 `cache/records`。

返回协议：

| status | 含义 | 路径 |
|---|---|---|
| cached | 已有成稿；实时模式检查了相关来源，历史模式仅检查快照存在 | report（run）或 store（批量） |
| prepared | 需模型填写草稿 | report、brief、draft、manifest |
| completed | 历史快照已由机器摘录生成 | report（run）或 index（批量） |
| published | 校验通过并发布 | report |

成功结果有 `checked_at`。run 的结果同时提供 `evidence_at`；历史读取不代表检查了最新来源，以 `mode=history` / `source_checked=false` 明确区分。准备或复用草稿同样记录本次检查时间。

## 报告库与兼容性

```text
<store>/
├── daily/YYYY-MM-DD.md (+ .meta.json)
├── weekly/YYYY-Www.md
├── monthly/YYYY-MM.md
├── yearly/YYYY.md
├── history-YYYY.md
├── evidence/<周期>/<日期>/<运行ID>/
├── evidence/backfill/<年>/<运行ID>/
└── cache/records/
```

旧数据原位保留，既有成稿里的绝对证据链接不需迁移。报告与元数据私有写入；源会话不进 Git。技能改名不修改已有日报内容。

## 新鲜度、发布与恢复

1. 来源签名涵盖文件 size/mtime/ctime/inode、SQLite WAL、Kimi state、Cursor workspace、解析器版本及默认时区标识。
2. 签名未变时复用成稿。变更时重新筛选相关证据；未变文件复用完整脱敏解析缓存，变化文件完整重解析。文件截断、轮转和依赖变化都会使对应缓存失效。失败/部分解析不缓存。
3. 相关证据未变时不要求模型重写。只有证据变化才生成新草稿。与目标日同 cwd 的新会话是后续候选，不自动证明旧任务完成。
4. 发布在目标锁内重新验证来源集合、原始来源签名、证据文件和目标报告。重验证只覆盖到报告的证据截止时间：晚于截止的追加（活跃会话的常规写入）属下一次更新，放行发布；早于截止但后写入的消息仍算相关变更，必须重新准备。活跃客户端不共享这把锁，因此不能承诺在发布完成后永远没有新消息。
5. 并发写入兼容：明文 JSONL 尾部未带换行的半行视为写入中，跳过且不计解析错误，下次采集自然补全；完整行的真实损坏仍如实告警。`.zstd` 等压缩归档不可能处于写入中，其尾部损坏按真实损坏告警，不适用半行规则。缓存安全性来自签名绑定文件状态（size/mtime/ctime/inode）：解析期间或半行状态下文件继续变化都会使签名失配，缓存不固化进行中的内容。
6. 自身会话排除：采集启动时固化标记——自动识别 `PI_SESSION_FILE` 与 `CLAUDE_CODE_SESSION_ID`（对应 `.claude/projects` 下同名文件），可用 `--self 绝对路径` 显式补充（宿主未设环境变量时必须显式传入，路径不存在会告警）；被标记且不含目标日消息的会话不作为证据或后续候选。排除清单记录于 index 的 `self_excluded` 并存入 manifest；发布重验证只按 manifest 清单执行，不再读当前进程环境，避免跨进程 env 漂移造成签名抖动。跨午夜、含目标日消息的自身会话保留目标日证据，仅其今日消息按普通后续处理。
7. 报告与元数据分别原子写入；二者并非跨文件事务。写入前的 `.publishing.json` 标记可识别中途失败，下一次重新准备，避免把不一致状态当缓存命中。成功后清除标记。
8. 损坏的元数据保留并警告，作为缓存未命中处理；有效的旧报告不删除。更新备份报告和元数据。
9. 遗留 `.lock` 不自动强删；核实 PID、机器和进程状态后移走锁。批次 status.json 记录完成列表；异常写 failed.json，强制断电至少留下 running 状态。重复命令跳过已完成快照，继续缺失日期；历史重建失败时仍需重复 `--rebuild`。

周期汇总只用保存的下级报告。年报优先月报，所以月报过期时须先更新月报；不能声称年报自动追溯所有原始会话。冻结历史版不会因新增来源自动改写。

## 成本与限制

冷缓存需要解析全部支持的历史，历史较多时可能耗时一分钟或更长，不能保证固定耗时。热缓存仍需遍历文件、加载规范化记录并筛选日期；这是文件级复用，不是字节偏移增量读取。缓存增加磁盘占用且包含脱敏后的历史正文，应保持私有权限。签名修改和解析器升级会使旧解析缓存失效。

脱敏只覆盖常见模式，不保证任意自然语言凭据都被发现，也可能误伤文本。`root /app/project` 等路径会保留；账号/密码形式使用更严格的分隔规则。输出前仍需检查，不能还原被脱敏字段。

所有 JSON/JSONL 按 UTF-8 读取。会话非法编码会记录来源异常，不悄悄用替换字符改变证据。普通来源失败不会阻断其他文件；覆盖表反映部分采集。

## 扩展与验证

增加来源时在 `scripts/sources.py` 注册发现规则和依赖，在适配器实现 `(session_id, cwd, role, aware_datetime, text, source_reference)`，保留行号/下标/主键。无可靠时间返回 None，不猜测。适配器新增读取的元数据必须进入依赖签名。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s <skill-dir>/tests -v
```

测试覆盖适配、脱敏、预算、缓存、异常恢复、并发、日期边界与历史切换；模型对项目归并、历史指令抵抗和证据强度判断仍依赖宿主，脚本测试不等于模型效果对照试验。
