"""季集校验用例：集号范围、季无分集数据、同季文件数超集数、原因码。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import MANIFEST, FakeHost, ctx_for  # noqa: E402

BASE = "media/inbox/unsorted"
TID = 95396


def file(ref: str, name: str, folder: str = "剧集") -> dict:
    return {"ref": ref, "name": name, "path": f"{BASE}/{folder}/{name}", "rootId": "r",
            "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""}


def reply(items: list[dict]) -> str:
    return json.dumps({"items": items})


def item(ref: str, season, episode, title="人生切割术", tmdb_id=TID) -> dict:
    return {"fileRef": ref, "title": title, "originalTitle": "Severance", "year": 2022,
            "mediaType": "tv", "tmdbId": tmdb_id, "season": season, "episode": episode,
            "multiEpisode": False, "sources": [], "reason": ""}


def detail(seasons: list[tuple[int, int]]) -> dict:
    return {"id": TID, "title": "人生切割术", "originalTitle": "Severance",
            "year": "2022", "mediaType": "tv", "overview": "",
            "seasons": [{"season_number": n, "episode_count": c} for n, c in seasons]}


def run(files: list[dict], items: list[dict], seasons: list[tuple[int, int]]):
    host = FakeHost(files, reply(items), {}, {(TID, "tv"): detail(seasons)})
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, files, {"batchSize": 8, "yearTolerance": 1, "maxMinutes": 20})
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    records = [v["value"] for k, v in sorted(host.kv.items()) if "/items/" in k]
    return host, records


def test_episode_out_of_range():
    """集号超过该季集数：必然错位，要拦。"""
    files = [file("a", "Show.S02E15.1080p.mkv")]
    host, records = run(files, [item("a", 2, 15)], [(0, 1), (1, 9), (2, 10)])
    r = records[0]
    assert r["status"] == "needs_review", r
    assert r["reasonCode"] == "episode_out_of_range", r
    assert "只有 10 集" in r["reason"] and "第 15 集" in r["reason"], r["reason"]
    assert not host.prepared, "不该生成草稿"
    print("  集号越界:", r["reason"])


def test_episode_in_range_passes():
    files = [file("a", "Show.S02E10.1080p.mkv")]
    host, records = run(files, [item("a", 2, 10)], [(1, 9), (2, 10)])
    assert records[0]["status"] in ("matched", "submitted"), records[0]
    assert records[0]["reasonCode"] == "", records[0]
    print("  集号在范围内: 通过 ✓")


def test_season_without_episodes():
    """该季集数为 0（未播出/无数据）：无法校验，转人工。"""
    files = [file("a", "Show.S03E05.1080p.mkv")]
    host, records = run(files, [item("a", 3, 5)], [(1, 9), (2, 10), (3, 0)])
    r = records[0]
    assert r["status"] == "needs_review", r
    assert r["reasonCode"] == "episode_unverifiable", r
    print("  该季无分集数据:", r["reason"])


def test_season_count_exceeds():
    """同季文件数超过 TMDB 集数：整组暂停（多份副本也算超）。"""
    files = [file("a", "Show.S01E01.1080p.mkv"),
             file("b", "Show.S01E02.1080p.mkv"),
             file("c", "Show.S01E02.480p.mkv")]
    host, records = run(
        files,
        [item("a", 1, 1), item("b", 1, 2), item("c", 1, 2)],
        [(1, 2)])
    assert len(records) == 3, records
    for r in records:
        assert r["status"] == "needs_review", r
        assert r["reasonCode"] == "season_count_mismatch", r
        assert "3 个文件" in r["reason"] and "只有 2 集" in r["reason"], r["reason"]
    assert not host.prepared
    print("  同季超量:", records[0]["reason"])


def test_season_count_partial_ok():
    """只处理部分集数是正常用法，不该拦。"""
    files = [file("a", "Show.S01E03.1080p.mkv")]
    host, records = run(files, [item("a", 1, 3)], [(1, 9)])
    assert records[0]["status"] in ("matched", "submitted"), records[0]
    print("  只处理部分集数: 通过 ✓")


def test_reason_codes_for_conflicts():
    """身份冲突类也要有稳定原因码。"""
    files = [file("a", "Show.S01E01.1080p.mkv")]
    wrong_title = [{"fileRef": "a", "title": "完全无关的剧", "originalTitle": "Unrelated",
                    "year": 2022, "mediaType": "tv", "tmdbId": TID, "season": 1,
                    "episode": 1, "multiEpisode": False, "sources": [], "reason": ""}]
    host, records = run(files, wrong_title, [(1, 9)])
    assert records[0]["status"] == "identity_conflict", records[0]
    assert records[0]["reasonCode"] == "title_conflict", records[0]

    wrong_year = [{"fileRef": "a", "title": "人生切割术", "originalTitle": "Severance",
                   "year": 1990, "mediaType": "tv", "tmdbId": TID, "season": 1,
                   "episode": 1, "multiEpisode": False, "sources": [], "reason": ""}]
    host, records = run(files, wrong_year, [(1, 9)])
    assert records[0]["reasonCode"] == "year_conflict", records[0]
    print("  冲突原因码: title_conflict / year_conflict ✓")


if __name__ == "__main__":
    for function in (test_episode_out_of_range, test_episode_in_range_passes,
                     test_season_without_episodes, test_season_count_exceeds,
                     test_season_count_partial_ok, test_reason_codes_for_conflicts):
        function()
    print("\n季集校验用例全部通过")
