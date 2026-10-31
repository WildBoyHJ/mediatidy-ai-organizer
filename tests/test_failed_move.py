"""失败目录用例：只移动整理不出来的文件，没配置就不动。"""
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
FAILED_DIR = {"rootId": "root-1", "path": "fail"}


def fresh():
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, DIRECT)
    return host, plugin, handlers, ctx


def records() -> list[dict]:
    return [
        {"ref": "a", "name": "a.mkv", "status": "unrecognized", "reason": "AI 没给"},
        {"ref": "b", "name": "b.mkv", "status": "identity_conflict"},
        {"ref": "c", "name": "c.mkv", "status": "needs_review"},
        {"ref": "d", "name": "d.mkv", "status": "matched"},
        {"ref": "e", "name": "e.mkv", "status": "submitted"},
    ]


def test_move_only_failed_states():
    """只动未识别/身份冲突/需人工，已核对与已提交不碰。"""
    host, plugin, handlers, ctx = fresh()
    rows = records()
    moved = plugin._move_failed(ctx, "flow-1", rows, FAILED_DIR)
    assert moved == 3, moved
    assert [item["fileRef"] for item in host.moved] == ["a", "b", "c"], host.moved
    assert host.moved[0]["destination"] == {"rootId": "root-1", "path": "fail"}
    assert rows[0]["movedTo"] == "fail" and rows[1]["movedTo"] == "fail"
    assert "movedTo" not in rows[3], "已核对的不该被移动"
    assert "movedTo" not in rows[4], "已提交的不该被移动"
    print("  移动:", [i["fileRef"] for i in host.moved])


def test_no_dir_does_nothing():
    host, plugin, handlers, ctx = fresh()
    rows = records()
    assert plugin._move_failed(ctx, "flow-1", rows, None) == 0
    assert plugin._move_failed(ctx, "flow-1", rows, {"rootId": "", "path": "x"}) == 0
    assert host.moved == [], host.moved
    print("  没配失败目录 → 不移动")


def test_move_failure_recorded():
    host, plugin, handlers, ctx = fresh()
    host.fail_move = True
    rows = records()
    moved = plugin._move_failed(ctx, "flow-1", rows, FAILED_DIR)
    assert moved == 0, moved
    assert rows[0]["moveError"] == "MOVE_DENIED", rows[0]
    print("  移动失败只记错误:", rows[0]["moveError"])


def test_already_moved_skipped():
    host, plugin, handlers, ctx = fresh()
    rows = records()
    rows[0]["movedTo"] = "fail"
    moved = plugin._move_failed(ctx, "flow-1", rows, FAILED_DIR)
    assert moved == 2, moved
    assert [i["fileRef"] for i in host.moved] == ["b", "c"], host.moved
    print("  已移动过的跳过 ✓")


def test_start_saves_failed_dir():
    host, plugin, handlers, ctx = fresh()
    handlers[("action", "start")](
        {"profileId": 7, "mode": "direct", "failedDir": FAILED_DIR}, ctx)
    flow = host.kv["flows/flow-1"]["value"]
    assert flow["failedDir"] == FAILED_DIR, flow.get("failedDir")
    settings = host.kv["settings"]["value"]
    assert settings["lastFailedDir"] == FAILED_DIR, settings
    print("  失败目录已随本轮落盘:", flow["failedDir"])


def test_start_without_failed_dir():
    host, plugin, handlers, ctx = fresh()
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    flow = host.kv["flows/flow-1"]["value"]
    assert flow["failedDir"] is None, flow.get("failedDir")
    print("  未选失败目录 → 落盘为 null")


def test_page_has_picker():
    host, plugin, handlers, ctx = fresh()
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    blob = json.dumps(page, ensure_ascii=False)
    assert '"DirectoryPicker"' in blob, "要有目录选择器"
    assert "state.failedDir" in blob, "目录选择器要绑 state.failedDir"
    assert "失败目录（可选）" in blob
    assert "access" in blob and "write" in blob, "选择器需要写权限"
    start = [a for a in MANIFEST["actions"] if a["id"] == "start"][0]
    assert "failedDir" in start["inputSchema"]["properties"], "start 要接收 failedDir"
    print("  页面选择器与清单入参就位")


def test_reason_shows_move_result():
    host, plugin, handlers, ctx = fresh()
    assert plugin._reason_with_move({"reason": "AI 没给", "movedTo": "fail"}) == \
        "AI 没给 · 已移到失败目录 fail"
    assert plugin._reason_with_move({"moveError": "MOVE_DENIED"}) == \
        "移到失败目录失败（MOVE_DENIED）"
    assert plugin._reason_with_move({"reason": "ok"}) == "ok"
    print("  说明列带移动结果 ✓")


if __name__ == "__main__":
    for function in (test_move_only_failed_states, test_no_dir_does_nothing,
                     test_move_failure_recorded, test_already_moved_skipped,
                     test_start_saves_failed_dir, test_start_without_failed_dir,
                     test_page_has_picker, test_reason_shows_move_result):
        function()
    print("\n失败目录用例全部通过")
