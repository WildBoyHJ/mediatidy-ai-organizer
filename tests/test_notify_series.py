"""按剧集聚合通知的用例：一部剧一条，而不是一集一条。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

import notify  # noqa: E402

COUNTS = {"total": 12, "matched": 10, "submitted": 10, "failed": 1, "moved": 1}


def items() -> list[dict]:
    rows = []
    for episode in (1, 2, 3):
        rows.append({"status": "organized", "tmdbId": 100, "title": "人生切割术",
                     "season": 2, "episode": episode, "name": f"e{episode}.mkv"})
    for episode in (5, 6):
        rows.append({"status": "submitted", "tmdbId": 200, "title": "沙丘",
                     "season": 1, "episode": episode, "name": f"d{episode}.mkv"})
    rows.append({"status": "failed", "title": "坏文件", "name": "bad.mkv"})
    rows.append({"status": "unrecognized", "title": "", "name": "unknown.mkv"})
    return rows


def test_one_message_per_series():
    messages = notify.build_series(COUNTS, "direct", "临时整理", items())
    assert len(messages) == 2, [m[0] for m in messages]
    titles = [title for title, _ in messages]
    assert any("人生切割术" in t for t in titles), titles
    assert any("沙丘" in t for t in titles), titles
    print("  剧集条数:", len(messages), "|", titles)


def test_body_has_episode_range():
    pairs = notify.build_series(COUNTS, "direct", "临时整理", items())
    title, body = next((t, b) for t, b in pairs if "人生切割术" in t)
    assert "第 2 季" in title, title
    assert "共 3 个文件" in body, body
    assert "3 集（1–3）" in body, body
    assert "直接整理" in body, body
    assert "失败文件已移到失败目录" in body, body
    print("  标题:", title)
    for line in body.splitlines():
        print("   ", line)


def test_failed_not_included():
    messages = notify.build_series(COUNTS, "draft", "p", items())
    joined = "\n".join(title + body for title, body in messages)
    assert "坏文件" not in joined, "失败的不该出现在入库通知里"
    assert "unknown" not in joined, "未识别的不该出现"
    print("  失败/未识别未混入入库通知 ✓")


def test_single_episode_series():
    rows = [{"status": "matched", "tmdbId": 9, "title": "电影", "season": None,
             "episode": None, "name": "movie.mkv"}]
    messages = notify.build_series({"total": 1, "matched": 1}, "draft", "p", rows)
    assert len(messages) == 1
    title, body = messages[0]
    assert "第" not in title, title
    assert "集数" not in body, body
    print("  单片/单集不带季集号:", title)


def test_empty_when_nothing_organized():
    rows = [{"status": "failed", "name": "x.mkv"}]
    assert notify.build_series({"total": 1}, "direct", "p", rows) == []
    print("  没有入库记录 → 空列表（回退汇总）")


if __name__ == "__main__":
    for function in (test_one_message_per_series, test_body_has_episode_range,
                     test_failed_not_included, test_single_episode_series,
                     test_empty_when_nothing_organized):
        function()
    print("\n按剧集聚合通知用例全部通过")
