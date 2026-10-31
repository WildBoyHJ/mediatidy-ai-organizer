"""六步总览用例：扫描/识别/核对/提交/整理/归档 的状态与位置。"""
from __future__ import annotations

import json
import sys
import time
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
DIR = [{"ref": "d1", "name": "in", "path": ".", "rootId": "r", "isDir": True}]


class ScanHost(FakeHost):
    scan_status = "running"

    def m_files_scan(self, value):
        return {"ref": "scan-1", "runId": "s", "kind": "scan", "status": self.scan_status,
                "total": 0, "completed": 0, "failed": 0, "updatedAt": ""}

    def m_tasks_items(self, value):
        if value.get("offset"):
            return {"items": [], "nextOffset": 0}
        return {"items": [{"index": i, "status": "done", "file": f}
                          for i, f in enumerate(FILES)], "nextOffset": 0}


def now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(selection=None, host_cls=FakeHost):
    host = host_cls(FILES, AI_REPLY, SEARCH, DETAIL)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, selection if selection is not None else FILES, DIRECT)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)
    return host, plugin, handlers, ctx


def ask(handlers, name: str, ctx) -> dict:
    token = current.set(ctx)
    try:
        return handlers[("query", name)]({}, ctx)
    finally:
        current.reset(token)


def by_label(steps: list[dict]) -> dict[str, dict]:
    return {row["label"]: row for row in steps}


def test_scan_step_reads_mt_task():
    """选了目录：扫描步骤的进度要现查 MT 任务，不能是空白。"""
    host, plugin, handlers, ctx = run(DIR, ScanHost)
    host.extra_tasks = [{"ref": "scan-1", "runId": "s", "kind": "scan", "status": "running",
                         "total": 400, "completed": 120, "failed": 0, "updatedAt": ""}]
    data = ask(handlers, "progress", ctx)
    steps = by_label(data["steps"])
    assert steps["目录扫描"]["state"] == "进行中", steps["目录扫描"]
    assert "120" in steps["目录扫描"]["detail"], steps["目录扫描"]
    assert steps["AI 识别"]["state"] == "待开始", steps["AI 识别"]
    assert data["current"] == "目录扫描", data["current"]
    print("  扫描中:", steps["目录扫描"]["detail"])


def test_scan_step_skipped_when_files():
    host, plugin, handlers, ctx = run()
    steps = by_label(ask(handlers, "progress", ctx)["steps"])
    assert steps["目录扫描"]["state"] == "跳过", steps["目录扫描"]
    print("  直接选文件:", steps["目录扫描"]["state"])


def test_identify_step_position():
    host, plugin, handlers, ctx = run()
    host.kv["runs/flow-1/summary"]["value"]["status"] = "running"
    host.kv["runs/flow-1/progress"]["value"].update(
        {"stage": "识别中", "completed": 8, "total": 24, "at": now_stamp()})
    data = ask(handlers, "progress", ctx)
    steps = by_label(data["steps"])
    assert steps["AI 识别"]["state"] == "进行中", steps["AI 识别"]
    assert steps["AI 识别"]["detail"] == "8/24 个文件", steps["AI 识别"]
    assert steps["TMDB 核对"]["state"] == "待开始", steps["TMDB 核对"]
    assert data["completed"] == 8 and data["total"] == 24, data
    print("  识别中:", data["message"])


def test_verify_and_submit_steps():
    host, plugin, handlers, ctx = run()
    flow = host.kv["runs/flow-1/summary"]["value"]
    flow["status"] = "running"
    host.kv["runs/flow-1/progress"]["value"].update(
        {"stage": "核对 TMDB", "completed": 2, "total": 3, "at": now_stamp()})
    steps = by_label(ask(handlers, "progress", ctx)["steps"])
    assert steps["AI 识别"]["state"] == "已完成", steps["AI 识别"]
    assert steps["TMDB 核对"]["state"] == "进行中", steps["TMDB 核对"]
    assert steps["提交整理"]["state"] == "待开始", steps["提交整理"]
    print("  核对中:", steps["TMDB 核对"]["detail"])


def test_organize_step_running():
    """等整理时，MT 整理那一行要给出 已完成/进行中/排队。"""
    host, plugin, handlers, ctx = run()
    host.kv["runs/flow-1/summary"]["value"]["status"] = "waiting"
    host.task_status.update({"task-1": "done", "task-2": "running", "task-3": "queued"})
    data = ask(handlers, "progress", ctx)
    steps = by_label(data["steps"])
    assert steps["MT 整理"]["state"] == "进行中", steps["MT 整理"]
    assert "1/3" in steps["MT 整理"]["detail"], steps["MT 整理"]
    assert "进行中 1" in steps["MT 整理"]["detail"], steps["MT 整理"]
    assert steps["归档结果"]["state"] == "待开始", steps["归档结果"]
    print("  MT 整理:", steps["MT 整理"]["detail"])


def test_done_all_steps():
    host, plugin, handlers, ctx = run()
    host.kv["runs/flow-1/summary"]["value"]["status"] = "done"
    host.task_status.update({"task-1": "done", "task-2": "done", "task-3": "done"})
    steps = by_label(ask(handlers, "progress", ctx)["steps"])
    for label in ("AI 识别", "TMDB 核对", "提交整理", "MT 整理", "归档结果"):
        assert steps[label]["state"] == "已完成", (label, steps[label])
    print("  全部完成:", steps["归档结果"]["detail"])


def test_cancelled_step():
    host, plugin, handlers, ctx = run()
    host.kv["runs/flow-1/summary"]["value"]["status"] = "cancelled"
    host.task_status.update({f"task-{i}": "cancelled" for i in (1, 2, 3)})
    data = ask(handlers, "progress", ctx)
    steps = by_label(data["steps"])
    assert steps["MT 整理"]["state"] == "已取消", steps["MT 整理"]
    assert steps["归档结果"]["state"] == "已取消", steps["归档结果"]
    assert data["active"] is False, data
    print("  取消后:", data["message"])


def test_page_has_steps_table():
    host, plugin, handlers, ctx = run()
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    blob = json.dumps(page, ensure_ascii=False)
    assert "query.progress.data.steps" in blob, "进度卡要有六步表格"
    for header in ("步骤", "执行方", "状态", "进度"):
        assert header in blob, header
    assert "刷新进度" in blob, "要能手动刷新 MT 侧进度"
    print("  页面表格与刷新按钮就位")


if __name__ == "__main__":
    for function in (test_scan_step_reads_mt_task, test_scan_step_skipped_when_files,
                     test_identify_step_position, test_verify_and_submit_steps,
                     test_organize_step_running, test_done_all_steps,
                     test_cancelled_step, test_page_has_steps_table):
        function()
    print("\n六步总览用例全部通过")
