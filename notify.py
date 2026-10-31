"""整理结果通知：把每轮的成功/失败汇总发到宿主已配置的通知通道。"""
from __future__ import annotations

import re

MAX_LINES = 20
MAX_TITLE = 120


def _safe(text: str, limit: int = 200) -> str:
    """通知内容避免换行注入与超长。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit]


def build_summary(counts: dict, mode: str, profile_name: str, items: list[dict]) -> tuple[str, str]:
    """返回 (标题, 正文)。items 只需 name/title/status/reason。"""
    total = int(counts.get("total") or 0)
    organized = int(counts.get("submitted") or 0)
    matched = int(counts.get("matched") or 0)
    failed = int(counts.get("failed") or 0)
    unrecognized = int(counts.get("unrecognized") or 0)
    conflict = int(counts.get("identity_conflict") or 0)
    review = int(counts.get("needs_review") or 0)

    mode_label = "直接整理" if mode == "direct" else "草稿审阅"
    head = f"AI 识别整理 · {mode_label}"
    title = f"{head}｜成功 {organized}，未处理 {total - organized}"

    lines = [
        f"方案：{_safe(profile_name, 60) or '未知'}",
        f"总计 {total} 个文件",
        f"✅ 已提交整理：{organized}",
    ]
    if matched and not organized:
        lines.append(f"✅ 已核对可整理：{matched}")
    if failed:
        lines.append(f"❌ 失败：{failed}")
    if unrecognized:
        lines.append(f"❔ 未识别：{unrecognized}")
    if conflict:
        lines.append(f"⚠️ 身份冲突：{conflict}")
    if review:
        lines.append(f"👀 需人工处理：{review}")
    moved = int(counts.get("moved") or 0)
    if moved:
        lines.append(f"📦 已移到失败目录：{moved}")

    problems = [item for item in items if item.get("status") in
                ("failed", "identity_conflict", "needs_review")]
    if problems:
        lines.append("")
        lines.append("需要关注：")
        for item in problems[:MAX_LINES]:
            name = _safe(item.get("name", ""), 60)
            reason = _safe(item.get("reason", ""), 80)
            lines.append(f"· {name}" + (f" — {reason}" if reason else ""))
        if len(problems) > MAX_LINES:
            lines.append(f"… 另有 {len(problems) - MAX_LINES} 个")

    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[:3500] + "\n…（已截断）"
    return title[:MAX_TITLE], body


def build_progress_note(stage: str, detail: str = "") -> tuple[str, str]:
    """关键阶段的简短通知，避免刷屏。"""
    mapping = {
        "started": ("AI 识别整理 · 已开始", "任务已启动，识别完成后会再通知你。"),
        "scan_capped": ("AI 识别整理 · 目录超量", detail or "目录文件数超过单轮上限，已处理部分文件。"),
    }
    return mapping.get(stage, (f"AI 识别整理 · {_safe(stage, 30)}", _safe(detail, 500)))
