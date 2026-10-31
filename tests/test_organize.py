"""整理进度用例：任务映射落盘、状态汇总、当前整理项、页面声明。"""
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


def query_organize(plugin, handlers, ctx) -> dict:
    token = current.set(ctx)
    try:
        return handlers[("query", "organize")]({}, ctx)
    finally:
        current.reset(token)


def test_mapping_from_persisted_items():
    """映射直接取自落盘的识别结果里的 taskRef，不额外存一份。"""
    host, plugin, handlers, ctx = run(DIRECT)
    assert not [k for k in host.kv if "/taskmap/" in k], "不应再单独存 taskmap"
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    with_task = [r for r in records if r.get("taskRef")]
    assert len(with_task) == len(host.queued) == 3, (len(with_task), len(host.queued))
    assert {r["taskRef"] for r in with_task} == {f"task-{i + 1}" for i in range(3)}
    data = query_organize(plugin, handlers, ctx)
    assert data["total"] == 3, data
    assert all(item["label"] for item in data["items"]), data["items"]
    print("  映射来自 items 落盘 | 条数:", data["total"], "| 示例:", with_task[0]["title"])


def test_progress_while_running():
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "done", "task-2": "running", "task-3": "queued"})
    data = query_organize(plugin, handlers, ctx)
    assert data["total"] == 3, data
    assert data["done"] == 1, data
    assert data["running"] == 1, data
    assert data["remaining"] == 2, data
    assert data["current"], "应给出正在整理的片名"
    assert "正在整理" in data["message"], data["message"]
    assert data["message"].startswith("整理中 1/3"), data["message"]
    labels = [item["statusLabel"] for item in data["items"]]
    assert "正在整理" in labels and "排队中" in labels, labels
    print("  message:", data["message"])
    print("  hint:", data["hint"])


def test_progress_all_done():
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "done", "task-2": "done", "task-3": "done"})
    data = query_organize(plugin, handlers, ctx)
    assert data["done"] == 3 and data["remaining"] == 0, data
    assert data["message"] == "整理完成 3/3", data["message"]
    print("  message:", data["message"])


def test_progress_counts_failures():
    host, plugin, handlers, ctx = run(
        DIRECT, {"task-1": "done", "task-2": "failed", "task-3": "done"})
    data = query_organize(plugin, handlers, ctx)
    assert data["done"] == 2 and data["failed"] == 1, data
    assert data["remaining"] == 0, data
    assert any(item["statusLabel"] == "失败" for item in data["items"]), data["items"]
    print("  完成", data["done"], "失败", data["failed"], "|", data["hint"])


def test_unknown_status_does_not_crash():
    """任务不在 tasks.list 里（老任务/已清理）时归到待处理，不能崩。"""
    host, plugin, handlers, ctx = run(DIRECT)
    host.task_status.clear()
    host.queued.clear()  # tasks.list 返回空
    data = query_organize(plugin, handlers, ctx)
    assert data["total"] == 3, data
    assert data["pending"] == 3, data
    assert data["message"].startswith("整理中 0/3"), data["message"]
    print("  未知状态归入待处理 ✓ |", data["hint"])


def test_draft_mode_has_no_tasks():
    host, plugin, handlers, ctx = run(DRAFT)
    data = query_organize(plugin, handlers, ctx)
    assert data["total"] == 0, data
    assert "没有整理任务" in data["message"], data["message"]
    print("  草稿模式:", data["message"])


def test_no_run_yet():
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, DIRECT)
    data = query_organize(plugin, handlers, ctx)
    assert data["total"] == 0 and "尚未" in data["message"], data
    print("  未运行:", data["message"])


def test_page_declares_organize_card():
    _, plugin, handlers, ctx = run(DIRECT)
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    text = json.dumps(page, ensure_ascii=False)
    assert "query.organize.data.message" in text, "整理进度卡片要绑定 organize 查询"
    assert "整理进度" in text, "应有「整理进度」卡片"
    assert "刷新整理进度" in text, "应有刷新按钮"
    manifest = json.loads((ROOT / "plugin.json").read_text("utf-8"))
    ids = [q["id"] for q in manifest["queries"]]
    assert "organize" in ids, ids
    print("  页面卡片与清单查询声明一致 | 查询:", ids)


if __name__ == "__main__":
    for function in (test_mapping_from_persisted_items, test_progress_while_running,
                     test_progress_all_done, test_progress_counts_failures,
                     test_unknown_status_does_not_crash, test_draft_mode_has_no_tasks,
                     test_no_run_yet, test_page_declares_organize_card):
        function()
    print("\n整理进度用例全部通过")
