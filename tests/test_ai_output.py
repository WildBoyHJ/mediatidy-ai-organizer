"""用真实 AI 回答回归：mediaType=unknown、有 ID 无季集、目录标注 TMDB ID。"""
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


def file(ref, name, path):
    return {"ref": ref, "name": name, "path": path, "rootId": "r",
            "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""}


def run(files, reply, details, seasons=None):
    # 季信息现在来自 tmdb.detail 的 seasons 字段（不再单独调 tmdb.season）
    for tid, numbers in (seasons or {}).items():
        for key, value in details.items():
            if key[0] == tid and isinstance(value, dict):
                value["seasons"] = [{"season_number": n, "episode_count": 100}
                                    for n in numbers]
    host = FakeHost(files, reply, {}, details)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, files)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    return host, records


def detail(tid, mt, title, orig, year):
    return {"id": tid, "title": title, "originalTitle": orig,
            "year": year, "mediaType": mt, "overview": ""}


def test_unknown_type_resolved_by_id():
    """模型给 unknown 但有 ID：用 TMDB 反查类型，不该直接放弃。"""
    files = [file("a", "Some.Movie.2024.1080p.mkv", f"{BASE}/Some Movie/x.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某电影", "originalTitle": "Some Movie", "year": 2024,
        "mediaType": "unknown", "tmdbId": 999001, "season": None, "episode": None,
        "multiEpisode": False, "sources": []}]})
    details = {(999001, "movie"): detail(999001, "movie", "某电影", "Some Movie", "2024")}
    host, records = run(files, reply, details)
    assert records[0]["status"] == "submitted", records[0]
    assert records[0]["mediaType"] == "movie", records[0]
    print("  mediaType=unknown 但有 ID: 反查类型成功 ✓")


def test_id_without_season_single_season():
    """有 ID 但无季集，且该剧只有一季：自动补 S01，仍缺集号则转人工。"""
    name = "Change.Change.S01.2024.mkv"
    files = [file("a", name, f"{BASE}/Change (2024) {{tmdb-1291658}}/{name}")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "Change", "originalTitle": "Change", "year": 2024,
        "mediaType": "tv", "tmdbId": 1291658, "season": None, "episode": None,
        "multiEpisode": False, "sources": [],
        "reason": "该 ID 确认对应 2024 年作品，但文件名未给出季集信息"}]})
    details = {(1291658, "tv"): detail(1291658, "tv", "Change", "Change", "2024")}
    host, records = run(files, reply, details, seasons={1291658: [1]})
    r = records[0]
    assert r["status"] == "needs_review", r
    assert r["season"] == 1, r
    assert "集数" in r["reason"] or "第 1 季" in r["reason"], r["reason"]
    print("  有 ID 无季集（单季）: 补 S01，缺集号转人工 ✓", r["reason"][:60])


def test_id_without_season_multi_season():
    """多季剧缺季号：不能瞎猜，列出真实季让用户选。"""
    files = [file("a", "Show.2020.mkv", f"{BASE}/Show/x.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某剧", "originalTitle": "Show", "year": 2020,
        "mediaType": "tv", "tmdbId": 999002, "season": None, "episode": None,
        "multiEpisode": False, "sources": []}]})
    details = {(999002, "tv"): detail(999002, "tv", "某剧", "Show", "2020")}
    host, records = run(files, reply, details, seasons={999002: [1, 2, 3]})
    r = records[0]
    assert r["status"] == "needs_review", r
    assert "S01" in r["reason"] and "S03" in r["reason"], r["reason"]
    print("  有 ID 无季号（多季）: 列出真实季转人工 ✓", r["reason"][:70])


def test_path_tmdb_hint():
    """目录名标注的 TMDB ID 应被提取，不依赖 AI。"""
    assert naming.tmdb_id_from_path(f"{BASE}/Change (2024) {{tmdb-1291658}}/x.mkv") == 1291658
    assert naming.tmdb_id_from_path(f"{BASE}/[tmdbid-12345]/x.mkv") == 12345
    assert naming.tmdb_id_from_path(f"{BASE}/无标记/x.mkv") == 0

    # 模型没给 ID，但路径有标注 → 用标注的 ID 继续核对
    files = [file("a", "Change.Change.S01E02.mkv",
                  f"{BASE}/Change (2024) {{tmdb-1291658}}/Season 1/Change.Change.S01E02.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "Change", "originalTitle": "Change", "year": 2024,
        "mediaType": "tv", "tmdbId": None, "season": 1, "episode": 2,
        "multiEpisode": False, "sources": []}]})
    details = {(1291658, "tv"): detail(1291658, "tv", "Change", "Change", "2024")}
    host, records = run(files, reply, details, seasons={1291658: [1]})
    r = records[0]
    assert r["tmdbId"] == 1291658, r
    assert "目录标注" in r["reason"] or r["status"] == "submitted", r
    print("  目录标注 TMDB ID: 自动采用 ✓", r["status"])


def test_no_id_no_hint_stays_unrecognized():
    """既没 ID 也没标注：老实标未识别，不猜。"""
    files = [file("a", "Cu.Zhi.2026.1080p.TX.mkv", f"{BASE}/租植/x.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "租植", "originalTitle": "", "year": 2026,
        "mediaType": "unknown", "tmdbId": None, "season": None, "episode": None,
        "multiEpisode": False, "sources": [],
        "reason": "无法确认对应中文作品及 TMDB 条目，避免猜测"}]})
    host, records = run(files, reply, {})
    r = records[0]
    assert r["status"] == "unrecognized", r
    assert "避免猜测" in r["reason"], r["reason"]
    assert not host.prepared
    print("  无 ID 无标注: 保持未识别 ✓")




# ---------------------------------------------------------------- 唯一候选兜底

class SearchHost(FakeHost):
    def __init__(self, search_results, *a, **kw):
        super().__init__(*a, **kw)
        self.search_results = search_results

    def m_tmdb_search(self, value):
        return self.search_results.get(value.get("query"), [])


def run_search(files, reply, search_results, details, seasons=None):
    host = SearchHost(search_results, files, reply, {}, details)
    host.seasons = seasons or {}
    host.m_tmdb_season = lambda v: host.seasons.get(v["id"], [])
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, files)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    return host, records


def test_ova_unique_match():
    """OVA 案例：模型因结构歧义不给 ID，但名称+年份唯一匹配时自动补上。"""
    name = "[魔神坛斗士][OVA2 辉煌帝传说][02][BDRip 1080p].mkv"
    files = [file("a", name, f"{BASE}/魔神坛斗士 OVA2/{name}")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "魔神坛斗士 OVA2 辉煌帝传说",
        "originalTitle": "鎧伝サムライトルーパー 輝煌帝伝説", "year": 1989,
        "mediaType": "tv", "tmdbId": None, "season": None, "episode": 2,
        "multiEpisode": False, "sources": [],
        "reason": "OVA 在 TMDB 的条目结构无法确认，故不给 ID"}]})
    search = {
        "魔神坛斗士 OVA2 辉煌帝传说": [
            {"id": 88801, "title": "魔神坛斗士 OVA2 辉煌帝传说",
             "originalTitle": "鎧伝サムライトルーパー 輝煌帝伝説",
             "year": "1989", "mediaType": "tv", "overview": ""}],
    }
    details = {(88801, "tv"): detail(88801, "tv", "魔神坛斗士 OVA2 辉煌帝传说",
                                     "鎧伝サムライトルーパー 輝煌帝伝説", "1989")}
    host, records = run_search(files, reply, search, details, seasons={88801: [1]})
    r = records[0]
    assert r["tmdbId"] == 88801, r
    assert r["status"] == "submitted", r
    assert "唯一匹配" in r["reason"] or r["status"] == "submitted"
    print("  OVA 唯一匹配: 自动补 ID 并整理 ✓")


def test_ambiguous_candidates_not_guessed():
    """多个候选都合格时不能猜，列出候选让用户判断。"""
    files = [file("a", "The.Perfect.Lie.S01E01.mkv", f"{BASE}/完美的谎言/x.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "完美的谎言", "originalTitle": "The Perfect Lie",
        "year": 2026, "mediaType": "tv", "tmdbId": None, "season": 1, "episode": 1,
        "multiEpisode": False, "sources": [], "reason": "同名候选无法裁决"}]})
    search = {
        "完美的谎言": [
            {"id": 7001, "title": "完美的谎言", "originalTitle": "The Perfect Lie",
             "year": "2026", "mediaType": "tv", "overview": ""},
            {"id": 7002, "title": "完美的谎言", "originalTitle": "The Perfect Lie",
             "year": "2026", "mediaType": "tv", "overview": ""},
        ],
    }
    host, records = run_search(files, reply, search, {})
    r = records[0]
    assert r["status"] == "unrecognized", r
    assert "同名候选无法裁决" in r["reason"], r["reason"]
    assert "7001" in r["reason"] and "7002" in r["reason"], "应列出候选 ID"
    assert not host.prepared
    print("  多候选: 不猜，列出候选 ✓")


def test_search_year_mismatch_rejected():
    """搜到的候选年份差太多，不能采纳。"""
    files = [file("a", "Show.2025.mkv", f"{BASE}/Show/x.mkv")]
    reply = json.dumps({"items": [{
        "fileRef": "a", "title": "某剧", "originalTitle": "Show", "year": 2025,
        "mediaType": "tv", "tmdbId": None, "season": 1, "episode": 1,
        "multiEpisode": False, "sources": []}]})
    search = {"某剧": [{"id": 7100, "title": "某剧", "originalTitle": "Show",
                        "year": "1998", "mediaType": "tv", "overview": ""}]}
    host, records = run_search(files, reply, search, {})
    r = records[0]
    assert r["status"] == "unrecognized", r
    assert not host.prepared
    print("  候选年份不符: 拒绝采纳 ✓")


if __name__ == "__main__":
    test_unknown_type_resolved_by_id()
    test_id_without_season_single_season()
    test_id_without_season_multi_season()
    test_path_tmdb_hint()
    test_no_id_no_hint_stays_unrecognized()
    test_ova_unique_match()
    test_ambiguous_candidates_not_guessed()
    test_search_year_mismatch_rejected()
    print("\nAI 输出回归全部通过")
