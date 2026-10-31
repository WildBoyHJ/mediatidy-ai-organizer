"""真实失败案例回归：用户实测报错的三类文件名。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

import naming  # noqa: E402
from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import MANIFEST, FakeHost, ctx_for  # noqa: E402

BASE = "media/inbox/unsorted"


def file(ref: str, name: str, path: str) -> dict:
    return {"ref": ref, "name": name, "path": path, "rootId": "r",
            "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""}


def run(files, reply, details, seasons=None, config=None):
    # 季信息现在来自 tmdb.detail 的 seasons 字段（不再单独调 tmdb.season）
    for tid, numbers in (seasons or {}).items():
        for key, value in details.items():
            if key[0] == tid and isinstance(value, dict):
                value["seasons"] = [{"season_number": n, "episode_count": 100}
                                    for n in numbers]
    host = FakeHost(files, reply, {}, details)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, files, config)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    return host, records


def detail(tmdb_id, media_type, title, original, year):
    return {"id": tmdb_id, "title": title, "originalTitle": original,
            "year": year, "mediaType": media_type, "overview": ""}


# ---------------------------------------------------------------- 本地解析

def test_local_parse():
    """三条真实文件名的本地解析结果。"""
    cases = [
        ("[完美的谎言].The.Perfect.Lie.2026.S01E01.1080p.Disney+.WEB-DL.AVC.AAC-LAOJI.mp4",
         {"title": "The Perfect Lie", "year": 2026, "mediaType": "tv",
          "season": 1, "episode": 1}),
        ("Hunter.with.a.Scalpel.S01.2025.1080p.friDay.WEB-DL.H264.AAC-COKEMV.mp4",
         {"title": "Hunter with a Scalpel", "year": 2025, "mediaType": "tv",
          "season": 1, "episode": 0}),
        ("Hunter.with.a.Scalpel.S02.2025.1080p.friDay.WEB-DL.H264.AAC-COKEMV.mp4",
         {"title": "Hunter with a Scalpel", "year": 2025, "mediaType": "tv",
          "season": 2, "episode": 0}),
    ]
    for name, want in cases:
        got = naming.parse(name)
        for key, value in want.items():
            assert got[key] == value, f"{name} 的 {key}: 期望 {value}，实际 {got[key]}"
    # 带集号的不能退化成 0
    assert naming.parse("Show.S01E05.1080p.mkv")["episode"] == 5
    print("  本地解析: 3 条真实文件名全部正确 ✓")


# ---------------------------------------------------------------- 三类报错

def test_ambiguous_same_name():
    """同名候选无法裁决：模型不给 ID 时标未识别，不硬选。"""
    name = "[完美的谎言].The.Perfect.Lie.2026.S01E01.1080p.Disney+.WEB-DL.AVC.AAC-LAOJI.mp4"
    path = f"{BASE}/[完美的谎言].The.Perfect.Lie.2026.S01.1080p.Disney+.WEB-DL.AVC.AAC-LAOJI/{name}"
    files = [file("a", name, path)]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "完美的谎言", "originalTitle": "The Perfect Lie",
        "year": 2026, "mediaType": "tv", "tmdbId": None, "season": 1, "episode": 1,
        "multiEpisode": False, "sources": [],
        "reason": "同名候选无法裁决：存在 2026 剧集与 2025 同名电影两个候选"}]})
    host, records = run(files, reply, {})
    assert records[0]["status"] == "unrecognized", records[0]
    assert "同名候选无法裁决" in records[0]["reason"], records[0]["reason"]
    assert not host.prepared
    print("  同名候选无法裁决: 标未识别并保留原因 ✓")


def test_missing_episode():
    """有季无集：不给 TMDB ID 也不自动整理，提示无法识别集数。"""
    name = "Hunter.with.a.Scalpel.S01.2025.1080p.friDay.WEB-DL.H264.AAC-COKEMV.mp4"
    path = f"{BASE}/拿着手术刀的猎人 (2025)/Season 1/{name}"
    files = [file("a", name, path)]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "拿着手术刀的猎人", "originalTitle": "Hunter with a Scalpel",
        "year": 2025, "mediaType": "tv", "tmdbId": 281967, "season": 1, "episode": None,
        "multiEpisode": False, "sources": [], "reason": "文件名只有季号，无集号"}]})
    details = {(281967, "tv"): detail(281967, "tv", "拿着手术刀的猎人",
                                      "Hunter with a Scalpel", "2025")}
    host, records = run(files, reply, details, seasons={281967: [1]})
    assert records[0]["status"] == "needs_review", records[0]
    assert "无法识别集数" in records[0]["reason"], records[0]["reason"]
    assert not host.prepared
    print("  有季无集: 转人工并说明 ✓", records[0]["reason"])


def test_season_not_exists():
    """季不存在：文件名 S02 但 TMDB 只有 1 季，要拦下并列出真实季。"""
    name = "Hunter.with.a.Scalpel.S02.2025.1080p.friDay.WEB-DL.H264.AAC-COKEMV.mp4"
    path = f"{BASE}/拿着手术刀的猎人 (2025)/Season 1/{name}"
    files = [file("a", name, path)]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "拿着手术刀的猎人", "originalTitle": "Hunter with a Scalpel",
        "year": 2025, "mediaType": "tv", "tmdbId": 281967, "season": 2, "episode": 1,
        "multiEpisode": False, "sources": [], "reason": ""}]})
    details = {(281967, "tv"): detail(281967, "tv", "拿着手术刀的猎人",
                                      "Hunter with a Scalpel", "2025")}
    host, records = run(files, reply, details, seasons={281967: [1]})
    assert records[0]["status"] == "needs_review", records[0]
    assert "第 2 季" in records[0]["reason"] and "S01" in records[0]["reason"], records[0]["reason"]
    assert not host.prepared
    print("  季不存在: 拦下并列出真实季 ✓", records[0]["reason"])


def test_valid_season_passes():
    """季号真实存在时正常通过，不被误拦。"""
    name = "Show.S02E03.1080p.mkv"
    files = [file("a", name, f"{BASE}/Show/Season 2/{name}")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某剧", "originalTitle": "Show", "year": 2020,
        "mediaType": "tv", "tmdbId": 555, "season": 2, "episode": 3,
        "multiEpisode": False, "sources": [], "reason": ""}]})
    details = {(555, "tv"): detail(555, "tv", "某剧", "Show", "2020")}
    host, records = run(files, reply, details, seasons={555: [0, 1, 2]})
    assert records[0]["status"] == "submitted", records[0]
    assert host.prepared[0]["season"] == 2 and host.prepared[0]["episode"] == 3
    print("  合法季号: 正常通过 ✓")


def test_season_query_failure_degrades():
    """季列表查询失败时不阻止整理，只跳过该项校验。"""
    name = "Show.S05E01.1080p.mkv"
    files = [file("a", name, f"{BASE}/Show/{name}")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某剧", "originalTitle": "Show", "year": 2020,
        "mediaType": "tv", "tmdbId": 556, "season": 5, "episode": 1,
        "multiEpisode": False, "sources": [], "reason": ""}]})
    details = {(556, "tv"): detail(556, "tv", "某剧", "Show", "2020")}
    host, records = run(files, reply, details, seasons={})  # 返回空列表
    assert records[0]["status"] == "submitted", records[0]
    print("  季列表不可用: 降级不误拦 ✓")


if __name__ == "__main__":
    test_local_parse()
    test_ambiguous_same_name()
    test_missing_episode()
    test_season_not_exists()
    test_valid_season_passes()
    test_season_query_failure_degrades()
    print("\n真实案例回归全部通过")
