"""进度条用例：阶段、文件名、单调递增、上报失败不影响主流程、页面绑定。"""
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

# 3 个视频（readme.txt 会被过滤掉），每批 2 个 → 两批，能验证进度真的在走
CONFIG = {"profileId": 7, "mode": "draft", "batchSize": 2,
          "yearTolerance": 1, "maxMinutes": 20, "nativeSearch": False}


def run(fail_progress: bool = False):
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    host.fail_progress = fail_progress
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES, CONFIG)
    result = handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    return host, result, plugin, ctx


def stages(host) -> list[str]:
    """把 message 里的阶段名抽出来（形如「识别中 2/3 · xxx.mkv」）。"""
    return [entry["message"].split(" ")[0] for entry in host.progress]


def test_stage_sequence():
    host, result, _, _ = run()
    seen = stages(host)
    assert host.progress, "必须上报过进度"
    for expected in ("准备识别", "识别中", "已识别", "核对", "生成草稿", "草稿已生成"):
        assert any(name.startswith(expected) for name in seen), f"缺少阶段 {expected}：{seen}"
    assert result["drafts"] == 3
    print("  阶段顺序:", " → ".join(dict.fromkeys(seen)))


def test_filename_in_message():
    host, _, _, _ = run()
    identifying = [e["message"] for e in host.progress if e["message"].startswith("识别中")]
    assert identifying, "应有识别中的进度"
    # 第一批 2 个，第二批 1 个：每批都要带出本批文件名
    assert any("Dune.Part.Two" in text for text in identifying), identifying
    assert any("Severance.S02E04" in text for text in identifying), identifying
    verifying = [e["message"] for e in host.progress if e["message"].startswith("核对")]
    assert any("Dune.Part.Two" in text for text in verifying), verifying
    print("  识别带文件名:", identifying[0])
    print("  核对带文件名:", verifying[0])


def test_monotonic_and_totals():
    host, _, _, _ = run()
    identify = [e for e in host.progress
                if e["message"].startswith(("识别中", "已识别"))]
    completed = [e["completed"] for e in identify]
    assert completed == sorted(completed), f"completed 必须单调不减：{completed}"
    assert all(e["total"] == 3 for e in identify), [e["total"] for e in identify]
    assert completed[-1] == 3, f"最后一批评完应到 3：{completed}"
    assert completed[0] == 0, f"开始时应为 0：{completed}"
    print("  completed 序列:", completed, "| total 恒为 3")


def test_long_filename_truncated():
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    long_name = "A" * 80 + ".mkv"
    host.files = [{**FILES[0], "name": long_name}] + FILES[1:]
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, host.files, CONFIG)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    for entry in host.progress:
        assert len(entry["message"]) < 120, entry["message"]
    assert any("…" in e["message"] for e in host.progress), "超长文件名应被截断加省略号"
    print("  长文件名已截断")


def test_progress_failure_does_not_break_run():
    host, result, _, _ = run(fail_progress=True)
    assert not host.progress, "上报失败时不应记录"
    assert result["drafts"] == 3, f"进度上报失败不能影响主流程：{result}"
    print("  上报全失败，仍生成 3 条草稿")


def test_page_binds_progress_label():
    _, _, plugin, ctx = run()
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
    finally:
        current.reset(token)
    text = json.dumps(page, ensure_ascii=False)
    assert "run.progress.completed" in text, "进度条应绑定 completed"
    assert "run.progress.total" in text, "进度条应绑定 total"
    assert "run.progress.message" in text, "进度条 label 应绑定 message"
    assert '"Progress"' in text, "页面必须有 Progress 组件"

    def walk(node):
        if isinstance(node, dict):
            if node.get("component") == "Progress":
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child)

    progress = list(walk(page))
    assert progress, "找不到 Progress 组件"
    props = progress[0].get("props") or {}
    assert "label" in props, f"Progress 缺少 label：{props}"
    print("  Progress props:", sorted(props))


if __name__ == "__main__":
    for function in (test_stage_sequence, test_filename_in_message, test_monotonic_and_totals,
                     test_long_filename_truncated, test_progress_failure_does_not_break_run,
                     test_page_binds_progress_label):
        function()
    print("\n进度用例全部通过")
