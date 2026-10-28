"""Adapters for Zcode desktop, Kimi (both generations), and DeepSeek Harness."""
import json
from pathlib import Path
import subprocess


def extra_records(path, source, warnings, stamp, text_of):
    sid, cwd = path.parent.name, None
    if source == "zcode-desktop":
        obj = json.loads(path.read_text())
        meta = obj.get("meta", {})
        for i, m in enumerate(obj.get("messages", [])):
            yield meta.get("taskId", path.stem), meta.get("workspacePath"), m.get("role"), stamp(m.get("timestamp")), text_of(m.get("content")), f"{path}#messages[{i}]"
        return
    if source.startswith("kimi"):
        for parent in [path.parent, *list(path.parents)[:3]]:
            state = parent / "state.json"
            if state.exists():
                try:
                    meta = json.loads(state.read_text())
                    cwd, sid = meta.get("cwd"), meta.get("id", parent.name)
                    break
                except (ValueError, OSError):
                    pass
        sid += "/" + path.parent.name if path.parent.parent.name == "agents" else ""
    proc = None
    if path.suffix == ".zstd":
        proc = subprocess.Popen(["zstd", "-dc", "--", str(path)], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, errors="replace")
        handle = proc.stdout
    else:
        handle = path.open(errors="replace")
    try:
        with handle:
            for n, line in enumerate(handle, 1):
                try:
                    o = json.loads(line)
                    kind, ts = o.get("type"), stamp(o.get("time", o.get("timestamp")))
                    role, text = None, ""
                    if source == "deepseek-harness":
                        if kind == "session":
                            sid, cwd = o.get("id", sid), o.get("cwd")
                        data = o.get("data", {})
                        if kind == "user/message":
                            role, text = "user", text_of(data.get("content"))
                        elif kind == "assistant/message":
                            role, text = "assistant", text_of(data.get("message", {}).get("content"))
                        elif kind == "turn/end" and data.get("reason", {}).get("kind") == "error":
                            role, text = "assistant", "[运行错误] " + json.dumps(data["reason"], ensure_ascii=False)
                    elif source == "kimi-code":
                        if kind == "profile.bind":
                            cwd = o.get("environmentDisclosure", {}).get("cwd", cwd)
                        if kind == "turn.prompt":
                            role, text = "user", text_of(o.get("input"))
                        elif kind == "context.append_loop_event":
                            e = o.get("event", {})
                            if e.get("type") == "content.part":
                                role, text = "assistant", text_of([e.get("part", {})])
                        elif kind == "turn.step.interrupted":
                            role, text = "assistant", "[中断] " + str(o.get("message", o.get("reason")))
                    elif source == "kimi":
                        m = o.get("message", {})
                        p = m.get("payload", {})
                        if m.get("type") == "TurnBegin":
                            role, text = "user", text_of(p.get("user_input"))
                        elif m.get("type") == "ContentPart":
                            role, text = "assistant", text_of([p])
                    if role and text:
                        yield sid, cwd, role, ts, text, f"{path}:{n}"
                except (ValueError, TypeError, AttributeError) as e:
                    warnings.append(f"{path}:{n}: {type(e).__name__}")
    finally:
        if proc and proc.wait() != 0:
            warnings.append(f"{path}: zstd 解压失败，可能只采集了部分记录")


def discover(home):
    """Inventory likely additional agents without reading settings, credentials, or histories."""
    names = ("trae", "windsurf", "cline", "roo", "copilot", "aider", "qwen", "crush", "goose", "augment", "deepseek")
    found = []
    for base in [home, home / ".config", home / ".local/share"]:
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if p.is_dir() and any(n in p.name.lower() for n in names):
                found.append({"path": str(p), "status": "发现候选目录，未解析；需检查是否有会话存储"})
    return found
