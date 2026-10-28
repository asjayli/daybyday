"""Compact, actionable coverage report; full evidence remains in coverage.json."""
LABELS = {
    "missing": "未找到存储", "active": "当日有活动", "partial": "部分覆盖",
    "no_activity_in_window": "历史可解析，当日无活动", "canonical_database_used": "已使用带时间数据库",
    "no_supported_messages": "有文件但无支持的消息", "no_session_files": "无会话文件",
    "resources_only": "仅技能资源，排除误报", "redirected": "已定位真实存储",
    "no_sessions": "无会话存储", "encrypted_store": "受阻：加密数据库", "opaque_store": "受阻：非明文数据库，格式未确认",
    "needs_adapter": "待适配", "needs_inspection": "待检查",
}


def unresolved(items):
    return [i for i in items if i.get("status") not in ("resources_only", "redirected", "no_sessions")]


def render(data):
    lines = [f"# {data['date']} 采集覆盖情况", "",
             f"窗口：{' → '.join(data['window'])}；截止：{data['collected_at']}", "",
             "“当日无活动”不等于解析失败。历史消息数与时间范围用于验证零命中；当日和后续消息分开计数。", "",
             "| 来源 | 判定 | 扫描文件 | 历史文本消息 | 当日 | 后续 | 最近消息时间 | 错误/无时间 |",
             "|---|---|---:|---:|---:|---:|---|---|" ]
    notes = []
    for name, s in data["coverage"].items():
        lines.append(f"| {name} | {LABELS.get(s.get('status'), '旧统计')} | {s['files_scanned']} | {s.get('total_messages', '未知')} | {s.get('target_messages', '未知')} | {s.get('followup_messages', '未知')} | {s.get('last_message_at') or '—'} | {s['errors']}/{s['undated_messages']} |")
        if s.get("mirrors_superseded"):
            notes.append(f"{name}：{s['mirrors_superseded']} 份会话 ID 已对应数据库，数据库作为主来源；导出镜像不重复计数，也不使用 mtime 补造消息时间。")
    lines.extend(["", *notes])
    lines.extend(["", "## 额外目录逐项核对", ""])
    for item in data.get("additional_candidates", []):
        lines.append(f"- `{item['path']}`：**{LABELS.get(item['status'], item['status'])}**。{item.get('reason', '')}")
        if item.get("session_store"):
            lines.append(f"  存储：`{item['session_store']}`。")
        if item.get("encryption_evidence"):
            lines.append(f"  加密证据：`{item['encryption_evidence']}`。")
        if item.get("next_action"):
            lines.append(f"  下一步：{item['next_action']}。")
    lines.extend(["", "## 仍需处理", ""])
    pending = unresolved(data.get("additional_candidates", []))
    lines.append(f"额外来源未闭环 {len(pending)} 项；解析警告 {len(data['warnings'])} 条；无时间源 {len(data['undated_sources'])} 个。")
    for warning in data["warnings"][:10]:
        lines.append(f"- {warning}")
    lines.extend(["", "完整路径、时间范围、分类证据与无时间明细见 [coverage.json](coverage.json)。", ""])
    return "\n".join(lines)
