"""整理任务用例：映射来源、待处理列表、汇总计数、当前整理项、页面声明。

说明：v0.11.1 起「整理进度」卡与「任务列表」合并，汇总信息统一由 tasks 查询提供，
不再有独立的 organize 查询，避免两张卡显示同一件事。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.core import current  # noqa: E402
from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import (  # noqa: E402
    AI_REPLY, DETAIL, FILES, MANIFEST, SEARCH, FakeHost, ctx_for,
)

DIRECT = {"profileId": 7, "mode": "direct", "batchSize": 8,
          "yearTolerance": 1, "maxMinutes": 20, "nativeSearch": False}
DRAFT = {**DIRECT, "mode": "draft"}


def run(config: dict, statuses: dict[str, str] | None = None):
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    if statuses:
        host.task_status.update(statuses)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, config)
    handlers[("action", "start")]({"profileId": 7, "mode": config["mode"]}, ctx)
    return host, plugin, handlers, ctx


def ask(handlers, name: str, ctx) -> dict:
    token = current.set(ctx)
    try:
        return handlers[("query", name)]({}, ctx)
    finally:
        current.reset(token)


def test_mapping_from_persisted_items():
    """映射直接取自落盘的识别结果里的 taskRef，不额外存一份。"""
    host, plugin, handlers, ctx = run(DIRECT)
    assert not [k for k in host.kv if "/taskmap/" in k], "不应再单独存 taskmap"
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    with_task = [r for r in records if r.get("taskRef")]
    assert len(with_task) == len(host.queued) == 3, (len(with_task), len(host.queued))
    data = ask(handlers, "tasks", ctx)
    assert data["total"] == 3, data
    print("  映射来自 items 落盘 | 条数:", data["total"])


def test_current_task_label():
    """tasks 查询要给出「正在整理」的那一部。"""
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "done", "task-2": "running", "task-3": "queued"})
    data = ask(handlers, "tasks", ctx)
    assert data["current"].startswith("正在整理 · "), data["current"]
    assert data["done"] == 1 and data["total"] == 2, data
    print("  ", data["current"], "|", data["message"])


def test_all_done_message():
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "done", "task-2": "done", "task-3": "done"})
    data = ask(handlers, "tasks", ctx)
    assert data["total"] == 0, data
    assert "已完成 3" in data["message"], data["message"]
    assert data["current"] == "", data
    print("  ", data["message"])


def test_failure_counted():
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "done", "task-2": "failed", "task-3": "done"})
    data = ask(handlers, "tasks", ctx)
    assert data["done"] == 2 and data["failed"] == 1, data
    print("  ", data["message"])


def test_all_cancelled_message():
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "cancelled", "task-2": "cancelled", "task-3": "cancelled"})
    data = ask(handlers, "tasks", ctx)
    assert data["total"] == 0 and data["cancelled"] == 3, data
    assert "已取消 3" in data["message"], data["message"]
    print("  ", data["message"])


def test_draft_mode_has_no_tasks():
    host, plugin, handlers, ctx = run(DRAFT)
    data = ask(handlers, "tasks", ctx)
    assert data["total"] == 0, data
    assert "没有整理任务" in data["message"], data["message"]
    print("  草稿模式:", data["message"])


def test_no_run_yet():
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, DIRECT)
    data = ask(handlers, "tasks", ctx)
    assert data["total"] == 0 and "没有整理任务" in data["message"], data
    assert data["current"] == "", data
    print("  未运行:", data["message"])


def test_no_duplicate_progress_card():
    """整理阶段只在 tasks 卡里体现，不再有第二张整理进度卡。"""
    _, plugin, handlers, ctx = run(DIRECT)
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    blob = json.dumps(page, ensure_ascii=False)
    assert "query.organize.data" not in blob, "不应再有独立的整理进度卡片"
    assert "query.tasks.data.items" in blob, "整理任务表格要绑定 tasks 查询"
    ids = [q["id"] for q in page["queries"]]
    assert ids == ["results", "tasks", "progress"], ids
    print("  查询列表:", ids, "（organize 已合并）")


if __name__ == "__main__":
    for function in (test_mapping_from_persisted_items, test_current_task_label,
                     test_all_done_message, test_failure_counted,
                     test_all_cancelled_message, test_draft_mode_has_no_tasks,
                     test_no_run_yet, test_no_duplicate_progress_card):
        function()
    print("\n整理任务用例全部通过")
