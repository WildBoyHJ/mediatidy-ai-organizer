"""识别结果按状态筛选的用例。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.core import current  # noqa: E402
from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import MANIFEST, FILES, FakeHost, ctx_for  # noqa: E402

DIRECT = {"profileId": 7, "mode": "direct", "batchSize": 8,
          "yearTolerance": 1, "maxMinutes": 20, "nativeSearch": False}

MIXED = [
    {"status": "matched", "name": "a.mkv", "title": "A"},
    {"status": "failed", "name": "b.mkv", "title": "B", "reason": "boom"},
    {"status": "matched", "name": "c.mkv", "title": "C"},
    {"status": "unrecognized", "name": "d.mkv", "title": ""},
    {"status": "failed", "name": "e.mkv", "title": "E", "reason": "boom2"},
]


def seed(statuses: list[str]):
    host = FakeHost(FILES, "{}", {}, {})
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, DIRECT)
    host.kv["currentRun"] = {"found": True, "value": "flow-1", "revision": "1", "bytes": 0}
    host.kv["runs/flow-1/summary"] = {
        "found": True, "revision": "1", "bytes": 0,
        "value": {"status": "done", "message": "识别完成",
                  "counts": {"total": len(statuses), "matched": 0, "submitted": 0, "failed": 0}},
    }
    for index, status in enumerate(statuses):
        host.kv[f"runs/flow-1/items/{index:06d}"] = {
            "found": True, "revision": "1", "bytes": 0,
            "value": {"ref": f"f{index}", "name": f"{index}.mkv", "status": status,
                      "title": f"T{index}", "mediaType": "movie"},
        }
    return host, plugin, handlers, ctx


def ask(handlers, ctx, status: str = "all", cursor: str = "") -> dict:
    token = current.set(ctx)
    try:
        return handlers[("query", "results")]({"status": status, "cursor": cursor}, ctx)
    finally:
        current.reset(token)


def test_all_returns_everything():
    host, plugin, handlers, ctx = seed([m["status"] for m in MIXED])
    data = ask(handlers, ctx)
    assert data["filter"] == "all", data
    assert len(data["items"]) == 5, len(data["items"])
    print("  全部:", len(data["items"]), "条")


def test_filter_by_status():
    host, plugin, handlers, ctx = seed([m["status"] for m in MIXED])
    failed = ask(handlers, ctx, "failed")
    assert failed["filter"] == "failed", failed
    assert len(failed["items"]) == 2, failed["items"]
    assert all(i["statusLabel"] == "失败" for i in failed["items"]), failed["items"]
    assert "只看「失败」" in failed["message"], failed["message"]
    matched = ask(handlers, ctx, "matched")
    assert len(matched["items"]) == 2, matched["items"]
    print("  失败:", len(failed["items"]), "| 已核对:", len(matched["items"]))


def test_empty_filter_result():
    host, plugin, handlers, ctx = seed(["matched", "matched"])
    data = ask(handlers, ctx, "failed")
    assert data["items"] == [], data
    assert data["filtered"] == 0, data
    print("  无命中时返回空列表，不报错")


def test_invalid_filter_falls_back():
    host, plugin, handlers, ctx = seed(["matched", "failed"])
    data = ask(handlers, ctx, "not-a-status")
    assert data["filter"] == "all", data
    assert len(data["items"]) == 2, data
    print("  非法取值回退到「全部」")


def test_filter_paging():
    """筛选后超过 20 条要能翻页，且不重复。"""
    host, plugin, handlers, ctx = seed(["failed"] * 25 + ["matched"] * 3)
    first = ask(handlers, ctx, "failed")
    assert len(first["items"]) == 20, len(first["items"])
    assert first["nextCursor"], first
    second = ask(handlers, ctx, "failed", first["nextCursor"])
    assert len(second["items"]) == 5, len(second["items"])
    ids_first = {i["id"] for i in first["items"]}
    ids_second = {i["id"] for i in second["items"]}
    assert not (ids_first & ids_second), "两页不能重复"
    print("  筛选分页 25 条：首页 20 + 次页 5 ✓")


def test_page_has_filter_control():
    host, plugin, handlers, ctx = seed(["matched"])
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    blob = json.dumps(page, ensure_ascii=False)
    assert "state.resultFilter" in blob, "筛选框要绑定 state.resultFilter"
    assert "按状态筛选" in blob
    for value in ("matched", "failed", "unrecognized", "identity_conflict"):
        assert value in blob, value
    assert "searchable" in blob, "表格应开启搜索"
    print("  筛选下拉与搜索框就位")


if __name__ == "__main__":
    for function in (test_all_returns_everything, test_filter_by_status,
                     test_empty_filter_result, test_invalid_filter_falls_back,
                     test_filter_paging, test_page_has_filter_control):
        function()
    print("\n状态筛选用例全部通过")
