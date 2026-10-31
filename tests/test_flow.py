"""续接路径、同剧一致性、页面与查询的补充用例。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import (  # noqa: E402
    AI_REPLY, DETAIL, FILES, MANIFEST, SEARCH, FakeHost, ctx_for,
)

SCANNED = [
    {"ref": "s1", "name": "Oppenheimer.2023.1080p.BluRay.x264.mkv", "path": "in/Oppenheimer.2023.mkv",
     "rootId": "r", "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
    {"ref": "s2", "name": "The.Bear.S03E01.720p.mkv", "path": "in/The.Bear.S03E01.720p.mkv",
     "rootId": "r", "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
    {"ref": "s3", "name": "cover.jpg", "path": "in/cover.jpg", "rootId": "r",
     "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
]


class ScanHost(FakeHost):
    scan_status = "queued"

    def m_files_scan(self, value):
        return {"ref": "scan-1", "runId": "s", "kind": "scan", "status": self.scan_status,
                "total": 0, "completed": 0, "failed": 0, "updatedAt": ""}

    def m_tasks_items(self, value):
        if value.get("offset"):
            return {"items": [], "nextOffset": 0}
        return {"items": [{"index": i, "status": "done", "file": f}
                          for i, f in enumerate(SCANNED)], "nextOffset": 0}


def load():
    return load_plugin(ROOT, MANIFEST)


def test_scan_continuation():
    search = {
        "Oppenheimer": [{"id": 872585, "title": "奥本海默", "originalTitle": "Oppenheimer",
                         "year": "2023", "mediaType": "movie", "overview": ""}],
        "The Bear": [{"id": 136315, "title": "熊家餐馆", "originalTitle": "The Bear",
                      "year": "2022", "mediaType": "tv", "overview": ""}],
    }
    detail = {
        (872585, "movie"): {"id": 872585, "title": "奥本海默", "originalTitle": "Oppenheimer",
                            "year": "2023", "mediaType": "movie", "overview": ""},
        (136315, "tv"): {"id": 136315, "title": "熊家餐馆", "originalTitle": "The Bear",
                         "year": "2022", "mediaType": "tv", "overview": ""},
    }
    # 扫描到的文件由 AI 直接给 tmdbId
    reply = json.dumps({"items": [
        {"fileRef": "s1", "title": "奥本海默", "originalTitle": "Oppenheimer", "year": 2023,
         "mediaType": "movie", "tmdbId": 872585, "multiEpisode": False, "sources": []},
        {"fileRef": "s2", "title": "熊家餐馆", "originalTitle": "The Bear", "year": 2022,
         "mediaType": "tv", "tmdbId": 136315, "season": 3, "episode": 1,
         "multiEpisode": False, "sources": []},
    ]}, ensure_ascii=False)
    host = ScanHost([], reply, search, detail)
    plugin, handlers = load()
    folder = {"ref": "dir", "name": "in", "path": "in", "rootId": "r", "isDir": True,
              "version": "", "size": 0, "modifiedAt": ""}
    ctx = ctx_for(host, [folder])
    first = handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    assert first.get("waiting") is True, first
    assert host.waiting == ["scan-1"], host.waiting
    assert any(n == "files.scan" for n, _ in host.calls), "应提交扫描任务"

    # 续接：宿主把扫描任务结果交给插件
    pending = handlers[("continuation", "after-scan")]
    ctx2 = ctx_for(host, [])
    ctx2.data["event"] = {"tasks": [{"ref": "scan-1", "status": "done"}]}
    ctx2.data["runId"] = "run-1"
    out = pending({"flowKey": "flow-1"}, ctx2)
    # cover.jpg 不是视频 → 只处理 2 个视频
    # 草稿条目只带 fileRef 与识别信息，文件名要从扫描结果反查
    scanned = {f["ref"]: f["name"] for f in SCANNED}
    names = {scanned.get(item["fileRef"], "?") for item in host.prepared}
    assert names == {"Oppenheimer.2023.1080p.BluRay.x264.mkv", "The.Bear.S03E01.720p.mkv"}, names
    assert "cover.jpg" not in json.dumps(out, ensure_ascii=False)
    print("  扫描续接: 处理", len(names), "个视频，跳过非视频 ✓")


def test_series_conflict():
    """同剧一致性是纯函数：不同 TMDB ID / 不同季号 / 不同目录分别验证。"""
    plugin, _ = load()
    base = {"status": "matched", "mediaType": "tv", "title": "Show", "rootId": "r",
            "season": 1, "episode": 1, "tmdbId": 111, "path": "Show/S01/x.mkv"}
    # 同目录、同名、不同 TMDB ID → 整组挂起
    a = dict(base, episode=1, tmdbId=111)
    b = dict(base, episode=2, tmdbId=222)
    out = plugin._check_series([dict(a), dict(b)])
    assert all(r["status"] == "identity_conflict" for r in out), out

    # 同目录、同 ID、不同季号 → 整组挂起
    c = dict(base, season=1, tmdbId=111)
    d = dict(base, season=2, tmdbId=111)
    out = plugin._check_series([dict(c), dict(d)])
    assert all(r["status"] == "identity_conflict" for r in out), out

    # 同 ID 同季 → 保留
    e = dict(base, episode=1, tmdbId=111)
    f = dict(base, episode=2, tmdbId=111)
    out = plugin._check_series([dict(e), dict(f)])
    assert all(r["status"] == "matched" for r in out), out

    # 不同目录的同名剧（不同季各自目录）不互相牵连
    g = dict(base, season=1, tmdbId=111, path="Show/S01/x.mkv")
    h = dict(base, season=2, tmdbId=222, path="Show/S02/x.mkv")
    out = plugin._check_series([dict(g), dict(h)])
    assert all(r["status"] == "matched" for r in out), out
    print("  同剧一致性: 冲突/季号/跨目录规则正确 ✓")


def test_page_and_query():
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL)
    plugin, handlers = load()
    ctx = ctx_for(host, FILES)
    page = handlers_page(plugin, ctx)
    assert page["version"] == "2"
    assert page["queries"] == [{"id": "results", "name": "results",
                                "input": {"cursor": {"$ref": "state.cursor"}}}]
    assert "cursor" in page["state"]
    blob_early = json.dumps(page, ensure_ascii=False)
    # 早期宿主会拒绝的属性不能出现在页面里
    for banned in ("\"loading\"",):
        assert banned not in blob_early, f"页面不应使用 {banned}"
    # profile 与 mode 必须 bind 到 input 空间，否则 invoke 时 inputSchema 缺必填字段
    assert "input.profileId" in blob_early, "profileId 必须 bind 到 input.profileId"
    assert "input.mode" in blob_early, "mode 必须 bind 到 input.mode"
    assert "ProfilePicker" in blob_early, "应使用 ProfilePicker 选择整理方案"
    # 页面里所有 $ref 必须指向已声明的数据源
    blob = json.dumps(page, ensure_ascii=False)
    assert "query.results.data.items" in blob
    empty = handlers[("query", "results")]({}, ctx)
    assert empty["items"] == [] and "尚未开始" in empty["message"], empty

    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    listed = handlers[("query", "results")]({}, ctx)
    assert len(listed["items"]) == 3, listed
    assert all(item["statusLabel"] for item in listed["items"])
    assert listed["message"].startswith("已生成"), listed["message"]
    print("  页面与查询: 声明一致，空态与结果态正常 ✓")


def handlers_page(plugin, ctx):
    from mt_plugin.core import current
    token = current.set(ctx)
    try:
        return plugin.get_page(ctx)
    finally:
        current.reset(token)


def test_scan_failed_keeps_partial():
    """扫描任务 failed（撞上限）时，仍要使用已经扫到的文件。"""
    reply = json.dumps({"items": [
        {"fileRef": "s1", "title": "奥本海默", "originalTitle": "Oppenheimer", "year": 2023,
         "mediaType": "movie", "tmdbId": 872585, "multiEpisode": False, "sources": []},
    ]}, ensure_ascii=False)
    detail = {(872585, "movie"): {"id": 872585, "title": "奥本海默", "originalTitle": "Oppenheimer",
                                  "year": "2023", "mediaType": "movie", "overview": ""}}
    host = ScanHost([], reply, {}, detail)
    plugin, handlers = load()
    folder = {"ref": "dir", "name": "in", "path": "in", "rootId": "r", "isDir": True,
              "version": "", "size": 0, "modifiedAt": ""}
    ctx = ctx_for(host, [folder])
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)

    ctx2 = ctx_for(host, [])
    # 关键：任务状态是 failed，但 items 里有 2 个文件
    ctx2.data["event"] = {"tasks": [{"ref": "scan-1", "status": "failed", "completed": 500}]}
    out = handlers[("continuation", "after-scan")]({"flowKey": "flow-1"}, ctx2)
    scanned = {f["ref"]: f["name"] for f in SCANNED}
    names = {scanned.get(item["fileRef"], "?") for item in host.prepared}
    assert "Oppenheimer.2023.1080p.BluRay.x264.mkv" in names, names
    assert "cover.jpg" not in names, "非视频仍要跳过"
    print("  扫描失败保留部分结果: 用了", len(names), "个文件 ✓")




def test_direct_mode_collect_flowkey():
    """直接整理：tasks.wait 必须带 flowKey，否则 collect 续接会报缺字段。"""
    reply = json.dumps({"items": [
        {"fileRef": "s1", "title": "奥本海默", "originalTitle": "Oppenheimer", "year": 2023,
         "mediaType": "movie", "tmdbId": 872585, "multiEpisode": False, "sources": []},
    ]}, ensure_ascii=False)
    detail = {(872585, "movie"): {"id": 872585, "title": "奥本海默",
                                  "originalTitle": "Oppenheimer", "year": "2023",
                                  "mediaType": "movie", "overview": ""}}
    files = [dict(SCANNED[0], ref="s1")]
    host = ScanHost(files, reply, {}, detail)
    plugin, handlers = load()
    ctx = ctx_for(host, files)
    handlers[("action", "start")]({"profileId": 7, "mode": "direct"}, ctx)

    # 找到 collect 的 tasks.wait 调用，确认带了 flowKey
    waits = [v for n, v in host.calls if n == "tasks.wait"]
    assert waits, "直接整理必须登记 tasks.wait"
    completion = waits[-1].get("completion") or {}
    assert completion.get("action") == "collect", completion
    assert "flowKey" in (completion.get("input") or {}), \
        f"collect 的 input 缺 flowKey：{completion}"

    # collect 续接能正常执行并归档
    ctx2 = ctx_for(host, [])
    ctx2.data["event"] = {"tasks": [{"ref": "task-1", "status": "done"}]}
    out = handlers[("continuation", "collect")]({"flowKey": "flow-1"}, ctx2)
    assert out["settled"] == 1, out
    print("  直接整理: collect 续接带 flowKey 并归档 ✓")


if __name__ == "__main__":
    test_scan_continuation()
    test_scan_failed_keeps_partial()
    test_series_conflict()
    test_page_and_query()
    test_direct_mode_collect_flowkey()
    print("\n补充用例全部通过")
