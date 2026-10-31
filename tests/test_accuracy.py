"""识别正确性专项：覆盖同名作品、错 ID、类型不符等误判场景。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import MANIFEST, FakeHost, ctx_for  # noqa: E402


def file(ref: str, name: str, path: str = "") -> dict:
    return {"ref": ref, "name": name, "path": path or f"in/{name}", "rootId": "r",
            "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""}


def detail(tmdb_id: int, media_type: str, title: str, original: str, year: str) -> dict:
    return {"id": tmdb_id, "title": title, "originalTitle": original,
            "year": year, "mediaType": media_type, "overview": ""}


def run(files, reply, details, config=None):
    host = FakeHost(files, reply, {}, details)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, files, config)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    return host, records


def case_wrong_id():
    """AI 给了错误的 TMDB ID：名称对不上，必须拦下而不是照单整理。"""
    files = [file("a", "The.Office.US.S02E01.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "办公室", "originalTitle": "The Office", "year": 2005,
        "mediaType": "tv", "tmdbId": 2316, "season": 2, "episode": 1,
        "multiEpisode": False, "reason": "", "sources": []}]})
    # 2316 是英版 The Office（2001），模型本意是美版 2316 之外的条目
    details = {(2316, "tv"): detail(2316, "tv", "办公室", "The Office", "2001")}
    host, records = run(files, reply, details)
    assert records[0]["status"] == "identity_conflict", records[0]
    assert "2001" in records[0]["reason"], records[0]["reason"]
    assert not host.prepared
    print("  错 ID + 年份不符: 拦下 ✓", records[0]["reason"])


def case_same_name_different_show():
    """同名不同作品：ID 指向另一部，名称交集失败要拦。"""
    files = [file("a", "Battlestar.Galactica.S01E01.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "太空堡垒卡拉狄加", "originalTitle": "Battlestar Galactica",
        "year": 2004, "mediaType": "tv", "tmdbId": 1972, "season": 1, "episode": 1,
        "multiEpisode": False, "reason": "", "sources": []}]})
    # 1972 = 1978 老版
    details = {(1972, "tv"): detail(1972, "tv", "太空堡垒卡拉狄加", "Battlestar Galactica", "1978")}
    host, records = run(files, reply, details)
    assert records[0]["status"] == "identity_conflict", records[0]
    assert "1978" in records[0]["reason"], records[0]["reason"]
    print("  同名不同年代: 拦下 ✓", records[0]["reason"])


def case_type_mismatch():
    """电影与同名剧集：mediaType 与 TMDB 返回不一致要拦。"""
    files = [file("a", "Fargo.1996.1080p.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "冰血暴", "originalTitle": "Fargo", "year": 2014,
        "mediaType": "movie", "tmdbId": 60625, "season": None, "episode": None,
        "multiEpisode": False, "reason": "", "sources": []}]})
    details = {(60625, "movie"): detail(60625, "tv", "冰血暴", "Fargo", "2014")}
    host, records = run(files, reply, details)
    assert records[0]["status"] in ("identity_conflict", "failed"), records[0]
    assert not host.prepared
    print("  类型不符: 拦下 ✓", records[0]["reason"])


def case_no_id_no_guess():
    """模型不给 ID 时不猜：标未识别，不去搜索替代作品。"""
    files = [file("a", "Some.Obscure.Show.S01E01.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某个冷门剧", "originalTitle": "", "year": None,
        "mediaType": "tv", "tmdbId": None, "season": 1, "episode": 1,
        "multiEpisode": False, "reason": "无法确认具体条目", "sources": []}]})
    host, records = run(files, reply, {})
    assert records[0]["status"] == "unrecognized", records[0]
    assert not host.prepared, "搜不到唯一候选就不能整理"
    # 允许做一次受控搜索，但绝不能凭空产生 ID
    assert records[0]["tmdbId"] == 0, records[0]
    print("  无 ID 不猜: 标未识别 ✓")


def case_multi_episode():
    """多集合集：标记 multiEpisode 时不自动整理。"""
    files = [file("a", "Show.S01E01-E03.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某剧", "originalTitle": "Show", "year": 2020,
        "mediaType": "tv", "tmdbId": 999, "season": 1, "episode": 1,
        "multiEpisode": True, "reason": "包含第 1-3 集", "sources": []}]})
    details = {(999, "tv"): detail(999, "tv", "某剧", "Show", "2020")}
    host, records = run(files, reply, details)
    assert records[0]["status"] == "needs_review", records[0]
    assert not host.prepared
    print("  多集合集: 转人工 ✓")


def case_alias_match():
    """模型给中文名、TMDB 原文名对得上 → 正常通过。"""
    files = [file("a", "Severance.S02E03.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "人生切割术", "originalTitle": "Severance", "year": 2022,
        "mediaType": "tv", "tmdbId": 95396, "season": 2, "episode": 3,
        "multiEpisode": False, "reason": "", "sources": []}]})
    details = {(95396, "tv"): detail(95396, "tv", "人生切割术", "Severance", "2022")}
    host, records = run(files, reply, details)
    assert records[0]["status"] == "submitted", records[0]
    assert host.prepared[0]["tmdbId"] == 95396
    assert host.prepared[0]["recognition"]["title"] == "人生切割术"
    print("  中文名 + 原文名: 通过 ✓")


if __name__ == "__main__":
    case_wrong_id()
    case_same_name_different_show()
    case_type_mismatch()
    case_no_id_no_guess()
    case_multi_episode()
    case_alias_match()
    print("\n误判专项用例全部通过")
