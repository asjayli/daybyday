#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Read local agent transcripts and produce traceable, dated evidence (no LLM/network)."""
import argparse
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo
from adapters import extra_records
from local_stores import cursor_records, cursor_session_ids, inventory


ROOTS = {
    "pi": ".pi/agent/sessions",
    "claude": ".claude/projects",
    "codex": ".codex/sessions",
    "gemini": ".gemini/tmp",
    "cursor": ".cursor/projects",
    "cursor-db": ".config/Cursor/User/globalStorage/state.vscdb",
    "opencode": ".local/share/opencode/opencode.db",
    "zcode": ".zcode/cli/db/db.sqlite",
    "zcode-desktop": ".zcode/v2/sessions",
    "kimi": ".kimi/sessions",
    "kimi-code": ".kimi-code/sessions",
    "deepseek-harness": ".dsh/sessions",
}


def stamp(value):
    try:
        if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value):
            value = float(value)
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text")
            and isinstance(p.get("text"), str)
        )
    return ""


def redact(text):
    # If a credential is named once, redact repeated mentions in the same message too.
    secrets = re.findall(r"(?i)\b(?:root|admin|postgres)\s*/\s*([^\s`，。；]+)", text)
    for value in secrets:
        if len(value) >= 4:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                  "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b", "[REDACTED]", text)
    text = re.sub(r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret)\b[\"']?\s*[:=]\s*)[\"']?[^\s\"',;]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(\b(?:root|admin|postgres)\s*/\s*)[^\s`，。；]+", r"\1[REDACTED]", text)
    text = re.sub(r"((?:密码|口令)(?:已[^\n。]{0,30})?(?:为|是|[:：])\s*)`?[^\s`，。；]+", r"\1[REDACTED]", text)
    text = re.sub(r"(://[^\s:/]+:)[^\s@/]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)


def records(path, source, warnings):
    """Yield normalized records, keeping source line numbers; never execute transcript content."""
    cwd, sid = None, path.stem
    if source == "gemini":
        try:
            obj = json.loads(path.read_text())
            for n, m in enumerate(obj.get("messages", [])):
                role = {"model": "assistant", "gemini": "assistant"}.get(m.get("type"), m.get("type"))
                yield obj.get("sessionId", sid), obj.get("cwd"), role, stamp(m.get("timestamp")), text_of(m.get("content")), f"{path}#messages[{n}]"
        except (ValueError, OSError, AttributeError, TypeError) as e:
            warnings.append(f"{path}: {type(e).__name__}")
        return
    with path.open(errors="replace") as handle:
        for n, line in enumerate(handle, 1):
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("record is not an object")
                if source == "codex":
                    p = obj.get("payload", {})
                    if obj.get("type") == "session_meta":
                        cwd, sid = p.get("cwd"), p.get("id", p.get("session_id", sid))
                    if obj.get("type") != "response_item" or p.get("type") != "message":
                        continue
                    m = p
                else:
                    cwd = obj.get("cwd", cwd)
                    sid = obj.get("sessionId", sid)
                    if obj.get("type") == "session":
                        sid = obj.get("id", sid)
                    m = obj.get("message", {})
                role = m.get("role", obj.get("role"))
                if role not in ("user", "assistant"):
                    continue
                yield sid, cwd, role, stamp(obj.get("timestamp", m.get("timestamp"))), text_of(m.get("content")), f"{path}:{n}"
            except (ValueError, AttributeError, TypeError) as e:
                warnings.append(f"{path}:{n}: {type(e).__name__}")


def db_records(path, start, now):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        query = """SELECT s.id,s.directory,m.id,m.time_created,m.data,p.id,p.data
                   FROM message m JOIN session s ON s.id=m.session_id
                   JOIN part p ON p.message_id=m.id
                   ORDER BY m.time_created,p.id"""
        for sid, cwd, mid, ts, raw, pid, part in db.execute(query):
            m, p = json.loads(raw), json.loads(part)
            if p.get("type") == "text" and not p.get("synthetic"):
                yield sid, cwd, m.get("role"), stamp(ts), p.get("text", ""), f"{path}#message={mid}&part={pid}"


def collect(home, overrides, start, end, now, max_chars=6000):
    sessions, coverage, warnings, uncertain = {}, {}, [], []
    cursor_db = overrides.get("cursor-db", home / ROOTS["cursor-db"])
    try:
        mirrored_ids = cursor_session_ids(cursor_db)
    except (sqlite3.Error, OSError) as exc:
        mirrored_ids = set()
        warnings.append(f"{cursor_db}: mirror lookup failed: {type(exc).__name__}")
    for source, default in ROOTS.items():
        root = overrides.get(source, home / default)
        stats = {"root": str(root), "exists": root.exists(), "files_scanned": 0,
                 "dated_messages": 0, "undated_messages": 0, "errors": 0,
                 "total_messages": 0, "target_messages": 0, "followup_messages": 0,
                 "first_message_at": None, "last_message_at": None,
                 "mirrors_superseded": 0, "status": "missing"}
        coverage[source] = stats
        before = len(warnings)
        if not root.exists():
            continue
        if source in ("opencode", "zcode", "cursor-db"):
            paths = [root]
        else:
            pattern = {"gemini": "session-*.json", "zcode-desktop": "session_*.json",
                       "kimi": "wire.jsonl", "kimi-code": "wire.jsonl",
                       "deepseek-harness": "session*.jsonl*"}.get(source, "*.jsonl")
            paths = sorted(root.rglob(pattern))
            if source == "cursor":
                paths = [p for p in paths if "agent-transcripts" in p.parts]
        for path in paths:
            if source == "cursor" and path.stem in mirrored_ids:
                stats["mirrors_superseded"] += 1
                stats["canonical_source"] = str(cursor_db)
                continue
            stats["files_scanned"] += 1
            undated = 0
            try:
                if source == "cursor-db":
                    stream = cursor_records(path, warnings, stamp)
                elif source in ("opencode", "zcode"):
                    stream = db_records(path, start, now)
                elif source in ("kimi", "kimi-code", "zcode-desktop", "deepseek-harness"):
                    stream = extra_records(path, source, warnings, stamp, text_of)
                else:
                    stream = records(path, source, warnings)
                for sid, cwd, role, ts, text, ref in stream:
                    if role not in ("user", "assistant") or not text.strip():
                        continue
                    stats["total_messages"] += 1
                    if ts is None:
                        undated += 1
                        continue
                    iso = ts.astimezone(start.tzinfo).isoformat()
                    if stats["first_message_at"] is None or ts < stamp(stats["first_message_at"]):
                        stats["first_message_at"] = iso
                    if stats["last_message_at"] is None or ts > stamp(stats["last_message_at"]):
                        stats["last_message_at"] = iso
                    if not start <= ts < now:
                        continue
                    stats["dated_messages"] += 1
                    stats["target_messages" if ts < end else "followup_messages"] += 1
                    key = (source, str(sid), str(path))
                    s = sessions.setdefault(key, {"agent": source, "session_id": sid, "cwd": cwd,
                                                   "source": str(path), "messages": []})
                    s["cwd"] = cwd or s["cwd"]
                    clean = redact(text)
                    clipped = max_chars > 0 and len(clean) > max_chars
                    if clipped:
                        half = max_chars // 2
                        clean = clean[:half] + "\n[中段省略；需按 source 回读]\n" + clean[-half:]
                    s["messages"].append({"at": ts.astimezone(start.tzinfo).isoformat(), "role": role,
                                          "period": "target" if ts < end else "followup",
                                          "text": clean, "truncated": clipped, "source": ref})
                if undated:
                    stats["undated_messages"] += undated
                    uncertain.append({"agent": source, "source": str(path), "messages": undated,
                                      "mtime": datetime.fromtimestamp(path.stat().st_mtime, start.tzinfo).isoformat(),
                                      "reason": "缺少带时区的消息时间；mtime 不能证明工作发生日期"})
            except (OSError, sqlite3.Error, ValueError, TypeError) as e:
                warnings.append(f"{path}: {type(e).__name__}: {e}")
        stats["errors"] = len(warnings) - before
        if stats["errors"] or stats["undated_messages"]:
            stats["status"] = "partial"
        elif stats["target_messages"]:
            stats["status"] = "active"
        elif stats["total_messages"]:
            stats["status"] = "no_activity_in_window"
        elif stats["mirrors_superseded"]:
            stats["status"] = "canonical_database_used"
        elif stats["files_scanned"]:
            stats["status"] = "no_supported_messages"
        else:
            stats["status"] = "no_session_files"
    selected = []
    for s in sessions.values():
        seen = set()
        unique = []
        for m in s["messages"]:
            key = (m["at"], m["role"], m["text"])
            if key not in seen:
                seen.add(key)
                unique.append(m)
        s["messages"] = unique
        if any(m["period"] == "target" for m in s["messages"]):
            s["messages"].sort(key=lambda m: m["at"])
            selected.append(s)
    selected.sort(key=lambda s: (s["cwd"] or "", s["messages"][0]["at"]))
    projects = {s["cwd"] for s in selected if s["cwd"]}
    active_session = os.environ.get("PI_SESSION_FILE")
    continuations = []
    for s in sessions.values():
        if (s["cwd"] in projects and s["source"] != active_session
                and not any(m["period"] == "target" for m in s["messages"])):
            s["messages"].sort(key=lambda m: m["at"])
            s["continuation_only"] = True
            continuations.append(s)
    selected.extend(sorted(continuations, key=lambda s: (s["cwd"], s["messages"][0]["at"])))
    return {"date": start.date().isoformat(), "window": [start.isoformat(), end.isoformat()],
            "collected_at": now.isoformat(), "coverage": coverage, "warnings": warnings,
            "undated_sources": uncertain, "additional_candidates": inventory(home), "sessions": selected}


def save(result, out):
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence = out / "sessions"
    evidence.mkdir(mode=0o700)
    index = []
    for i, session in enumerate(result.pop("sessions"), 1):
        name = f"{i:03d}-{session['agent']}-{hashlib.sha256(session['source'].encode()).hexdigest()[:10]}.json"
        p = evidence / name
        p.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n")
        messages = session["messages"]
        target = [m for m in messages if m["period"] == "target"]
        excerpt = target or messages
        index.append({k: v for k, v in session.items() if k != "messages"} | {
            "evidence": str(p), "target_messages": len(target), "followup_messages": len(messages)-len(target),
            "truncated_messages": sum(m["truncated"] for m in messages),
            "first": excerpt[0]["at"], "last": excerpt[-1]["at"],
            "opening": excerpt[0]["text"][:350], "ending": excerpt[-1]["text"][-650:]})
    result["sessions"] = index
    (out / "index.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return len(index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD，默认本地昨天")
    parser.add_argument("--timezone", help="IANA 时区，如 Asia/Shanghai；默认系统时区")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--root", action="append", default=[], metavar="AGENT=PATH", help="覆盖任一 agent 的数据根目录")
    parser.add_argument("--out", type=Path, help="新的证据输出目录（不得已存在）")
    parser.add_argument("--max-chars", type=int, default=6000, help="每条消息字符上限；0 为完整保留")
    args = parser.parse_args()
    if args.max_chars < 0:
        parser.error("--max-chars 必须 >= 0")
    tz = ZoneInfo(args.timezone) if args.timezone else None
    now = datetime.now(tz).astimezone(tz)
    day = date.fromisoformat(args.date) if args.date else now.date()-timedelta(days=1)
    if day >= now.date():
        parser.error("--date 必须早于今天")
    # astimezone(None) resolves each local midnight separately, including DST boundaries.
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(tz)
    end = datetime.combine(day+timedelta(days=1), time.min, tzinfo=tz).astimezone(tz)
    overrides = {}
    for item in args.root:
        key, sep, value = item.partition("=")
        if not sep or key not in ROOTS:
            parser.error("--root 格式为 AGENT=PATH；AGENT: " + ", ".join(ROOTS))
        overrides[key] = Path(value).expanduser().resolve()
    out = (args.out or args.home / ".local/share/daily-recap" / f"{day}-{now.strftime('%H%M%S-%f')}").expanduser().resolve()
    if out.exists():
        parser.error("输出目录已存在，请使用新目录以保留旧报告")
    os.umask(0o077)
    result = collect(args.home.expanduser(), overrides, start, end, now, args.max_chars)
    count = save(result, out)
    print(json.dumps({"index": str(out / "index.json"), "sessions": count,
                      "warnings": len(result["warnings"]), "coverage": result["coverage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
