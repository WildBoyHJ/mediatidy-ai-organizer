"""任务列表与取消用例：分页、单条取消、取消全部、越界保护、页面声明。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin import SDKError  # noqa: E402
from mt_plugin.core import current  # noqa: E402
from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import (  # noqa: E402
    AI_REPLY, DETAIL, FILES, MANIFEST, SEARCH, FakeHost, ctx_for,
)

DIRECT = {"profileId": 7, "mode": "direct", "batchSize": 8,
          "yearTolerance": 1, "maxMinutes": 20, "nativeSearch": False}


def run(statuses: dict[str, str] | None = None):
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    if statuses:
        host.task_status.update(statuses)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    return host, plugin, handlers, ctx


def add_tasks(host, extra: int) -> None:
    """在 3 个真实任务之外补出 extra 个任务，用来验证分页。"""
    for index in range(3, 3 + extra):
        ref = f"task-{index + 1}"
        host.queued.append({"fileRef": ref, "profileId": 7})
        host.kv[f"runs/flow-1/items/{index:06d}"] = {
            "found": True, "revision": "1", "bytes": 0,
            "value": {"ref": ref, "taskRef": ref, "name": f"Show.S01E{index:02d}.mkv",
                      "title": "补出来的剧", "mediaType": "tv", "season": 1, "episode": index},
        }


def ask(handlers, name: str, ctx, query_input: dict | None = None) -> dict:
    token = current.set(ctx)
    try:
        return handlers[("query", name)](query_input or {}, ctx)
    finally:
        current.reset(token)


def act(handlers, name: str, ctx, payload: dict) -> dict:
    token = current.set(ctx)
    try:
        return handlers[("action", name)](payload, ctx)
    finally:
        current.reset(token)


def test_list_only_pending():
    """已完成/失败/已取消的不占列表位，只留进行中与等待中。"""
    host, plugin, handlers, ctx = run(
        {"task-1": "done", "task-2": "cancelled", "task-3": "queued"})
    data = ask(handlers, "tasks", ctx)
    refs = [item["ref"] for item in data["items"]]
    assert refs == ["task-3"], refs
    assert data["total"] == 1, data
    assert data["done"] == 1 and data["cancelled"] == 1, data
    print("  列表只剩待处理:", data["message"])


def test_cancelled_task_disappears_after_cancel():
    """取消一个任务后刷新列表，它应当从列表里消失。"""
    host, plugin, handlers, ctx = run(
        {"task-1": "done", "task-2": "queued", "task-3": "running"})
    before = ask(handlers, "tasks", ctx)
    assert before["total"] == 2, before
    act(handlers, "cancel-task", ctx, {"ref": "task-2"})
    after = ask(handlers, "tasks", ctx)
    refs = [item["ref"] for item in after["items"]]
    assert "task-2" not in refs, f"已取消的任务不该再显示：{refs}"
    assert after["total"] == 1, after
    assert after["cancelled"] == 1, after
    print("  取消后列表:", refs, "|", after["message"])


def test_paging():
    host, plugin, handlers, ctx = run()
    add_tasks(host, 22)  # 共 25 个
    first = ask(handlers, "tasks", ctx, {"cursor": ""})
    assert first["total"] == 25, first["total"]
    assert len(first["items"]) == 20, len(first["items"])
    assert first["nextCursor"] == "20", first
    second = ask(handlers, "tasks", ctx, {"cursor": first["nextCursor"]})
    assert len(second["items"]) == 5, second
    assert second["nextCursor"] == "", second
    print("  分页 25 个：首页 20 + 次页 5 ✓")


def test_cancel_single():
    host, plugin, handlers, ctx = run()
    result = act(handlers, "cancel-task", ctx, {"ref": "task-2"})
    assert result == {"ref": "task-2", "cancelled": True}, result
    assert host.cancelled == ["task-2"], host.cancelled
    assert host.task_status["task-2"] == "cancelled"
    print("  单条取消:", result)


def test_cancel_rejects_foreign_ref():
    host, plugin, handlers, ctx = run()
    try:
        act(handlers, "cancel-task", ctx, {"ref": "task-999"})
    except SDKError as error:
        assert error.code == "TASK_NOT_IN_FLOW", error.code
        print("  越界 ref 被拒:", error.code)
        return
    raise AssertionError("不属于本轮的任务必须拒绝")


def test_cancel_requires_ref():
    host, plugin, handlers, ctx = run()
    try:
        act(handlers, "cancel-task", ctx, {"ref": ""})
    except SDKError as error:
        assert error.code == "NO_TASK_REF", error.code
        print("  空 ref 被拒:", error.code)
        return
    raise AssertionError("缺少 ref 必须拒绝")


def test_cancel_all_skips_finished():
    host, plugin, handlers, ctx = run(
        {"task-1": "done", "task-2": "queued", "task-3": "running"})
    result = act(handlers, "cancel-all", ctx, {})
    assert result["cancelled"] == 2, result
    assert sorted(host.cancelled) == ["task-2", "task-3"], host.cancelled
    assert "task-1" not in host.cancelled, "已完成的任务不能被取消"
    print("  取消全部:", result)


def test_cancel_all_failure_does_not_raise():
    host, plugin, handlers, ctx = run({"task-1": "queued", "task-2": "queued"})
    host.fail_cancel = True
    result = act(handlers, "cancel-all", ctx, {})
    assert result["cancelled"] == 0 and result["failed"] == 3, result
    print("  全部失败也不抛错:", result)


def test_manifest_and_page():
    manifest = json.loads((ROOT / "plugin.json").read_text("utf-8"))
    assert "tasks.cancel" in manifest["permissions"], manifest["permissions"]
    ids = {action["id"]: action for action in manifest["actions"]}
    assert "cancel-task" in ids and "cancel-all" in ids, list(ids)
    assert ids["cancel-task"]["inputSchema"]["required"] == ["ref"]
    assert ids["cancel-all"]["inputSchema"]["required"] == []

    _, plugin, handlers, ctx = run()
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    blob = json.dumps(page, ensure_ascii=False)
    for expected in ("任务列表", "取消全部未完成任务", "cancel-task", "cancel-all",
                     "query.tasks.data.items"):
        assert expected in blob, expected
    print("  清单与页面声明一致 ✓")


if __name__ == "__main__":
    for function in (test_list_only_pending, test_cancelled_task_disappears_after_cancel,
                     test_paging, test_cancel_single,
                     test_cancel_rejects_foreign_ref, test_cancel_requires_ref,
                     test_cancel_all_skips_finished,
                     test_cancel_all_failure_does_not_raise, test_manifest_and_page):
        function()
    print("\n任务取消用例全部通过")
