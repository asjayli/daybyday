#!/usr/bin/env python3
"""Batch immutable daily snapshots with explicit empty placeholders; one scan, zero LLM calls."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from brief import clean, clip, candidate
from collect import ROOTS, collect, save
from coverage_report import render as render_coverage

DEFAULT_STORE = Path.home() / ".local/share/daily-recap"


def local_zone(name=None):
    if name:
        return ZoneInfo(name)
    try:
        with Path("/etc/localtime").open("rb") as handle:
            return ZoneInfo.from_file(handle, key="local")
    except OSError:
        return datetime.now().astimezone().tzinfo


def days_between(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def frozen(target):
    meta = target.with_suffix(".meta.json")
    if not target.is_file() or not meta.is_file():
        return False
    data = json.loads(meta.read_text())
    return data.get("mode") == "history" and data.get("frozen") is True


def content_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def partition(sessions, wanted):
    """Use each message's local date, never session creation time or later completion."""
    result = defaultdict(list)
    for session in sessions:
        groups = defaultdict(list)
        for message in session["messages"]:
            day = datetime.fromisoformat(message["at"]).date()
            if day in wanted:
                groups[day].append(dict(message, period="target"))
        for day, messages in groups.items():
            copied = {k: v for k, v in session.items() if k not in ("messages", "continuation_only")}
            result[day].append(dict(copied, messages=sorted(messages, key=lambda m: m["at"])))
    for sessions in result.values():
        sessions.sort(key=lambda s: (s["cwd"] or "", s["messages"][0]["at"], s["source"]))
    return result


def excerpt(text, length=240):
    text = clean(text)
    text = re.sub(r"</?(?:user_query|user_info|environment_details)[^>]*>", "", text)
    return clip(text, length).replace("|", "\\|").replace("\n", " ")


def describe(session):
    users = [m for m in session["messages"] if m["role"] == "user" and clean(m["text"])
             and not clean(m["text"]).startswith(("Base directory for this skill:", "# AGENTS.md instructions", "<environment_context>"))]
    answers = [m for m in session["messages"] if m["role"] == "assistant" and clean(m["text"])]
    meaningful = [m for m in users if len(clean(m["text"])) >= 8]
    goal = excerpt((meaningful or users)[0]["text"], 180) if users else "当日无用户需求文本，可能是跨日或子代理会话"
    last = answers[-1] if answers else users[-1] if users else session["messages"][-1]
    return goal, last


def daily_report(day, sessions, evidence, coverage_path, generated_at, tz):
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day+timedelta(days=1), time.min, tzinfo=tz)
    total = sum(len(s["messages"]) for s in sessions)
    lines = [f"# {day} 日报", "", f"> 报告范围：{start.isoformat()} 至 {end.isoformat()}（左闭右开）",
             f"> 生成时间：{generated_at}", f"> 证据截至：{end.isoformat()}（不含此刻及之后消息）",
             "> 模式：历史快照，生成后固定；不检查后续变化",
             "> 状态：已完成" + ("（空白占位：未找到可归日记录）" if not sessions else "（机器摘录版）"), "",
             "本报告由本地脚本生成，未调用模型。引用是当日会话陈述，不等于重新核实的交付；不引入后来进展。", "",
             "## 工作进展", ""]
    groups = defaultdict(list)
    for i, s in enumerate(sessions, 1):
        groups[s["cwd"] or "项目未记录"].append((i, s))
    if not sessions:
        lines.append("**空白占位**：在目前可读取的 agent 会话中，没有找到当日文本消息。不能据此断言当天没有工作。")
    for project, items in groups.items():
        messages = sum(len(s["messages"]) for _, s in items)
        agents = ", ".join(sorted({s["agent"] for _, s in items}))
        latest_i, latest = max(items, key=lambda pair: pair[1]["messages"][-1]["at"])
        goal, last = describe(latest)
        lines.extend([f"### {project}", "", f"{len(items)} 个会话记录单元，{messages} 条消息；来源：{agents}。",
                      f"最近会话目标摘录：{goal}",
                      f"当日末次可见记录（S{latest_i:03d}，{last['at'][11:19]}）：{excerpt(last['text'], 400)}", ""])
    lines.extend(["", "## 断点与未闭环", ""])
    pending = []
    for i, s in enumerate(sessions, 1):
        text = candidate(s["messages"], re.compile(r"未完成|尚未|未验证|阻塞|卡在|中断|待确认|blocked|remaining", re.I), 300)
        if text:
            pending.append(f"- S{i:03d} · `{s['cwd'] or '项目未记录'}`：{excerpt(text, 300)}")
    if pending:
        lines.extend(["以下仅为各会话末次答复的关键词候选，可能包含条件描述或已解决事项；不自动认定为待办，也不跨会话推断状态。", "", *pending])
    else:
        lines.append("无可提取的明确断点。" if not sessions else "末次答复没有提取到明确断点；不代表工作全部完成。各会话停点见下方索引。")
    lines.extend(["", "## 会话索引", ""])
    index = json.loads((evidence / "index.json").read_text())
    for i, (s, summary) in enumerate(zip(sessions, index["sessions"]), 1):
        goal, last = describe(s)
        lines.extend([f"### S{i:03d} · {s['agent']} · {s['cwd'] or '项目未记录'}", "",
                      f"- 目标：{goal}", f"- 停点摘录：{excerpt(last['text'], 320)}",
                      f"- 时间：{s['messages'][0]['at']} 至 {s['messages'][-1]['at']}；{len(s['messages'])} 条。",
                      f"- [当天证据]({summary['evidence']})；会话 `{s['session_id']}`；末次来源 `{last['source']}`。", ""])
    if not sessions:
        lines.append("无当日会话。")
    lines.extend(["", "## 覆盖与局限", "",
                  f"- 当日 {len(sessions)} 个记录单元、{total} 条文本消息；子代理不等于独立任务。按项目归组，不宣称语义去重。",
                  "- 只使用该日 00:00 至次日 00:00 的消息；跨午夜会话按消息拆分，后来的成功不能改写当天失败。",
                  "- 每条归档文本至多保留首尾 3000 字符，截断明确标记；完整内容可按原始来源定位。",
                  "- 当前仍保留的本地历史不等于完整工作史；无记录、已删除历史、无法读取的加密来源均可能造成空白。",
                  f"- [本批次来源覆盖与未闭环项]({coverage_path})；采集时的覆盖状态不是当时安装情况。",
                  "- 历史快照不会自动追踪源会话变化；仅明确重建才替换，并保留旧版。", ""])
    return "\n".join(lines)


def write_day(store, day, sessions, run, coverage, generated_at, tz, expected_hash):
    target = store / "daily" / f"{day}.md"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence = run / str(day)
    end = datetime.combine(day+timedelta(days=1), time.min, tzinfo=tz)
    payload = {"date": str(day), "window": [datetime.combine(day,time.min,tzinfo=tz).isoformat(), end.isoformat()],
               "collected_at": generated_at, "sessions": sessions}
    save(payload, evidence)
    text = daily_report(day, sessions, evidence, coverage, generated_at, tz)
    meta = {"mode": "history", "frozen": True, "date": str(day), "period": "day", "key": str(day),
            "start": str(day), "end": str(day), "placeholder": not sessions,
            "generated_at": generated_at, "evidence_at": end.isoformat(),
            "sources": [str(evidence / "index.json")], "coverage": str(coverage),
            "sessions": len(sessions), "messages": sum(len(s["messages"]) for s in sessions),
            "projects": len({s['cwd'] for s in sessions}), "report": str(target)}
    lock = target.with_suffix(".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        if content_hash(target) != expected_hash:
            raise RuntimeError(f"Concurrent report change: {target}")
        if target.exists():
            (evidence / "previous-report.md").write_bytes(target.read_bytes())
            if target.with_suffix(".meta.json").exists():
                (evidence / "previous-report.meta.json").write_bytes(target.with_suffix(".meta.json").read_bytes())
        staged = evidence / "report.tmp"
        staged.write_text(text)
        os.replace(staged, target)
        staged_meta = evidence / "meta.tmp"
        staged_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2)+"\n")
        os.replace(staged_meta, target.with_suffix(".meta.json"))
    finally:
        lock.unlink()
    return meta


def write_year_index(store, year, days):
    rows, statuses = [], Counter()
    for day in days:
        p = store / "daily" / f"{day}.md"
        m = json.loads(p.with_suffix(".meta.json").read_text())
        status = "空白占位" if m.get("placeholder") else "有记录"
        statuses[status] += 1
        rows.append(f"| [{day}](daily/{day}.md) | {status} | {m.get('projects', '—')} | {m.get('sessions', '—')} | {m.get('messages', '—')} |")
    text = [f"# {year} 历史日报索引", "", f"范围：{days[0]} 至 {days[-1]}；共 {len(days)} 天，其中 {statuses['有记录']} 天有记录、{statuses['空白占位']} 天空白占位。",
            "", "历史快照只反映当天消息；空白仅表示目前未找到可读取记录。今日和未来日期不生成占位。", "",
            "| 日期 | 状态 | 项目数 | 会话单元 | 消息数 |", "|---|---|---:|---:|---:|", *rows, ""]
    target = store / f"history-{year}.md"
    target.write_text("\n".join(text))
    return target, dict(statuses)


def backfill(args):
    os.umask(0o077)
    tz = local_zone(args.timezone)
    today = datetime.now(tz).date()
    start = date.fromisoformat(args.start) if args.start else date(args.year, 1, 1)
    end = date.fromisoformat(args.end) if args.end else min(date(args.year,12,31),today-timedelta(days=1))
    if start.year != args.year or end.year != args.year or end >= today or end < start:
        raise ValueError("日期必须同属指定年份，起止有序，且截止日期早于今天")
    days = list(days_between(start,end))
    store = args.store.expanduser().resolve()
    wanted = [d for d in days if args.rebuild or not frozen(store / "daily" / f"{d}.md")]
    if not wanted:
        return {"status": "cached", "days": len(days), "scanned": False, "reason": "全部为已完成历史快照；未检查源会话变化", "store": str(store)}
    overrides = {}
    for item in args.root:
        key, sep, value = item.partition("=")
        if not sep or key not in ROOTS:
            raise ValueError("--root 必须为已支持的 AGENT=PATH")
        overrides[key] = Path(value).expanduser().resolve()
    expected = {d: content_hash(store / "daily" / f"{d}.md") for d in wanted}
    run = store / "evidence/backfill" / str(args.year) / datetime.now().strftime("%Y%m%dT%H%M%S-%f")
    run.mkdir(parents=True,mode=0o700)
    generated_at = datetime.now(tz).isoformat()
    lower = datetime.combine(min(wanted),time.min,tzinfo=tz)
    upper = datetime.combine(max(wanted)+timedelta(days=1),time.min,tzinfo=tz)
    print(f"一次扫描：{lower.isoformat()} → {upper.isoformat()}；待生成 {len(wanted)} 天",file=sys.stderr,flush=True)
    result = collect(args.home.expanduser().resolve(),overrides,lower,upper,upper,max_chars=3000)
    partitions = partition(result.pop("sessions"),set(wanted))
    result["collected_at"] = generated_at
    result["mode"] = "history_batch"
    coverage = run / "coverage.md"
    coverage.write_text(render_coverage(result).replace(f"# {result['date']} 采集覆盖情况", f"# {args.year} 历史补报来源覆盖", 1))
    (run / "coverage.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    counts = Counter()
    for i, day in enumerate(wanted,1):
        meta = write_day(store,day,partitions.get(day,[]),run,coverage,generated_at,tz,expected[day])
        counts["placeholders" if meta["placeholder"] else "active_days"] += 1
        if i % 50 == 0 or i == len(wanted):
            print(f"已保存 {i}/{len(wanted)} 天",file=sys.stderr,flush=True)
    year_days = sorted(date.fromisoformat(p.stem) for p in (store / "daily").glob(f"{args.year}-??-??.md")
                       if frozen(p))
    index, totals = write_year_index(store,args.year,year_days)
    summary = {"status":"completed","start":str(start),"end":str(end),"generated":len(wanted),
               "reused":len(days)-len(wanted),**counts,"index":str(index),"coverage":str(coverage),
               "totals":totals,"scanned":True,"model_calls":0}
    (run / "summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year",type=int,required=True)
    parser.add_argument("--start",help="YYYY-MM-DD，默认当年1月1日")
    parser.add_argument("--end",help="YYYY-MM-DD，默认当年年底或昨天（取更早）")
    parser.add_argument("--store",type=Path,default=DEFAULT_STORE)
    parser.add_argument("--home",type=Path,default=Path.home())
    parser.add_argument("--timezone")
    parser.add_argument("--root",action="append",default=[])
    parser.add_argument("--rebuild",action="store_true",help="明确重建已有快照；旧版备份")
    args = parser.parse_args()
    print(json.dumps(backfill(args),ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
