"""刷新页面后不丢状态：选择项来自 storage、进度有兜底。"""
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


def fresh():
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    return host, plugin, handlers


def page_of(plugin, ctx) -> dict:
    token = current.set(ctx)
    try:
        return plugin.get_page(ctx)
    finally:
        current.reset(token)


def ask(handlers, name: str, ctx) -> dict:
    token = current.set(ctx)
    try:
        return handlers[("query", name)]({}, ctx)
    finally:
        current.reset(token)


def test_defaults_before_any_run():
    """没跑过任何一轮时，默认草稿模式、未选方案。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    page = page_of(plugin, ctx)
    assert page["state"]["mode"] == "draft", page["state"]
    assert page["state"]["profileId"] is None, page["state"]
    print("  首屏默认:", page["state"])


def test_selection_survives_refresh():
    """跑过一轮之后刷新，方案与提交方式要带回来（state 默认值取自 storage）。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)

    # 模拟刷新：同一个 host/storage，重新取页面
    page = page_of(plugin, ctx)
    assert page["state"]["profileId"] == 7, page["state"]
    assert page["state"]["mode"] == "direct", page["state"]
    print("  刷新后仍选中: 方案", page["state"]["profileId"], "/", page["state"]["mode"])


def test_progress_survives_refresh():
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)

    stored = host.kv.get("runs/flow-1/progress")
    assert stored, f"进度必须落盘才能兜住刷新：{list(host.kv)}"
    data = ask(handlers, "progress", ctx)
    assert data["total"] == 3, data
    assert data["message"], data
    assert data["completed"] >= 0, data
    print("  刷新后进度:", data["message"], "| 状态", data["status"], "| active", data["active"])


def test_progress_marks_active_while_running():
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    # 模拟「运行中」：把 summary 状态改成 running
    host.kv["runs/flow-1/summary"]["value"]["status"] = "running"
    data = ask(handlers, "progress", ctx)
    assert data["active"] is True, data
    # 结束后应转成非 active
    host.kv["runs/flow-1/summary"]["value"]["status"] = "done"
    assert ask(handlers, "progress", ctx)["active"] is False
    print("  active 随 summary 状态切换 ✓")


def test_page_binds_progress_to_query():
    """进度条只认 storage（query.progress），避免 run.* 残留值把进度条卡住。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    blob = json.dumps(page_of(plugin, ctx), ensure_ascii=False)
    assert "query.progress.data.completed" in blob, "进度条要绑 storage 兜底值"
    assert "query.progress.data.message" in blob
    assert "query.progress.data.active" in blob, "运行中判断要兼顾刷新后的状态"
    assert "run.progress.completed" not in blob, "不要再混用 run.*，会被残留值盖住"
    print("  进度条只绑 query.progress ✓")


def test_progress_full_bar_when_organizing():
    """进入整理阶段后识别已结束，进度条应满格并指向整理进度。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    data = ask(handlers, "progress", ctx)
    assert data["status"] in ("waiting", "submitting"), data
    assert data["completed"] == data["total"] == 3, data
    assert "识别完成" in data["message"] and "整理进度" in data["message"], data["message"]
    assert data["active"] is True, data
    print("  整理阶段:", data["message"])


def test_progress_throttled():
    """进度落盘要节流，不能每个文件都写一次。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    writes = [name for name, value in host.calls
              if name == "storage.set" and str(value.get("key", "")).endswith("/progress")]
    assert len(writes) <= 10, f"进度写入次数过多：{len(writes)}"
    print("  进度落盘次数:", len(writes), "（3 个文件的一轮）")


def test_waiting_with_settled_tasks_is_not_active():
    """任务都结束了但没走到 collect（被取消/中断）时不能一直算「运行中」，否则按钮永久禁用。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    host.kv["runs/flow-1/summary"]["value"]["status"] = "waiting"
    host.task_status.update({"task-1": "cancelled", "task-2": "cancelled", "task-3": "cancelled"})
    data = ask(handlers, "progress", ctx)
    assert data["active"] is False, data
    assert data["status"] == "done", data
    print("  任务全结束 → 不再算运行中:", data["message"])


def test_stale_running_is_not_active():
    """识别阶段长时间没有新进度（插件被重启/流程被杀）也要放开按钮。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    host.kv["runs/flow-1/summary"]["value"]["status"] = "running"
    host.kv["runs/flow-1/progress"]["value"]["at"] = "2020-01-01T00:00:00Z"
    data = ask(handlers, "progress", ctx)
    assert data["active"] is False, data
    assert "中断" in data["message"], data["message"]
    print("  上一次中断:", data["message"])


def test_cancel_all_closes_flow():
    """取消全部后 summary 要收尾，否则 summary.status 会一直停在 waiting。"""
    host, plugin, handlers = fresh()
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    host.task_status.update({"task-1": "queued", "task-2": "queued", "task-3": "queued"})
    token = current.set(ctx)
    try:
        handlers[("action", "cancel-all")]({}, ctx)
    finally:
        current.reset(token)
    summary = host.kv["runs/flow-1/summary"]["value"]
    assert summary["status"] == "cancelled", summary
    data = ask(handlers, "progress", ctx)
    assert data["active"] is False, data
    print("  取消全部后收尾:", summary["message"], "| active", data["active"])


if __name__ == "__main__":
    for function in (test_defaults_before_any_run, test_selection_survives_refresh,
                     test_progress_survives_refresh, test_progress_marks_active_while_running,
                     test_page_binds_progress_to_query, test_progress_full_bar_when_organizing,
                     test_progress_throttled, test_waiting_with_settled_tasks_is_not_active,
                     test_stale_running_is_not_active, test_cancel_all_closes_flow):
        function()
    print("\n刷新不丢状态用例全部通过")
