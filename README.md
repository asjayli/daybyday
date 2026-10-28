# Daybyday

从本机 AI agent 会话生成可追溯的工作回顾：昨日进展、当前断点和接续建议，以及历史日报、周报、月报和年报。

Python 负责只读采集、脱敏、缓存和压缩；宿主模型负责语义合并。历史批量补报是确定性摘录，不调用模型。支持 Pi、Claude Code、Codex、Cursor、OpenCode、Zcode、Kimi 和 DeepSeek Harness 等本地记录，覆盖限制见 [来源说明](references/sources.md)。

## 安装

依赖 Python 3.11+；读取 `.zstd` 会话另需系统 `zstd`。

```bash
git clone git@github.com:asjayli/daybyday.git ~/.agents/skills/daybyday
```

Pi 可发现 `~/.agents/skills/`。Claude Code 个人技能需额外入口，保持单一代码来源：

```bash
mkdir -p ~/.claude/skills
ln -s ~/.agents/skills/daybyday ~/.claude/skills/daybyday
```

如目标已存在，先确认指向，不用强制覆盖。其他宿主把本目录加入其技能发现路径；不要假定所有宿主都自动扫描 `.agents/skills`。安装后重载技能或开启新会话。

## 使用

向宿主说“昨天干了什么，今天从哪里接上”“整理上周工作”或“补今年的历史日报”。主工作流在 [SKILL.md](SKILL.md)。

也可直接准备材料：

```bash
python3 ~/.agents/skills/daybyday/scripts/run.py
python3 ~/.agents/skills/daybyday/scripts/backfill.py --year 2026
```

直接调用 run 返回 `prepared` 时需要模型填写草稿，再使用 `reports.py publish`；脚本不会凭空生成经过语义核实的日报。

默认报告库：已有 `~/.local/share/daily-recap/` 时沿用，否则使用 `~/.local/share/daybyday/`。技能改名不搬迁旧报告，避免破坏绝对证据链接。`--store` 可显式指定私有报告库。仓库仅保存代码、文档和合成测试，不保存真实会话或报告。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
git diff --check
```

已实现文件级解析复用、来源变化校验、预算限制、发布锁及中断恢复。冷缓存仍需全量解析；热缓存减少解析但不消除文件遍历。凭据脱敏不是完整的敏感信息识别器。

维护说明见 [修复记录](FIXES.md)。许可证见 [LICENSE](LICENSE)。
