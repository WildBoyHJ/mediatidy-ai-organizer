"""用假宿主跑通完整流程：不需要真 AI、真 TMDB、真整理。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.core import Context  # noqa: E402
from mt_plugin.worker import load_plugin  # noqa: E402

MANIFEST = json.loads((ROOT / "plugin.json").read_text("utf-8"))


class FakeHost:
    """按方法名返回预设数据，并记录调用。"""

    def __init__(self, files, ai_reply, tmdb_search, tmdb_detail, fail_ai=None):
        self.files = files
        self.ai_reply = ai_reply
        self.tmdb_search = tmdb_search
        self.tmdb_detail = tmdb_detail
        self.fail_ai = fail_ai
        self.fail_progress = False
        self.calls: list[tuple[str, dict]] = []
        self.kv: dict[str, dict] = {}
        self.summary: dict = {}
        self.prepared: list[dict] = []
        self.queued: list[dict] = []
        self.waiting: list[str] = []
        self.progress: list[dict] = []
        self.task_status: dict[str, str] = {}
        self.cancelled: list[str] = []
        self.fail_cancel = False
        self.extra_tasks: list[dict] = []
        self.moved: list[dict] = []
        self.fail_move = False

    def call(self, invocation, name, value):
        self.calls.append((name, value))
        handler = getattr(self, "m_" + name.replace(".", "_"), None)
        if handler is None:
            raise AssertionError(f"未模拟的方法 {name}")
        return handler(value)

    # ---- 宿主方法
    def m_runtime_info(self, value):
        return {"sdkVersion": "2", "instanceId": "i", "runId": "r", "action": "start",
                "pluginVersion": "0.1.0", "scheduleId": "", "parentRunId": "",
                "limits": {"maxWorkflowFiles": 500, "maxFileBatch": 100}}

    def m_runtime_progress(self, value):
        if self.fail_progress:
            from mt_plugin import SDKError
            raise SDKError("进度上报失败", "PROGRESS_BOOM")
        self.progress.append({
            "completed": value.get("completed"),
            "total": value.get("total"),
            "message": value.get("message", ""),
        })
        return True

    def m_runtime_checkpoint(self, value):
        return True

    def m_runtime_result(self, value):
        self.summary = value.get("value") or {}
        return True

    def m_log(self, value):
        return True

    def m_ai_capabilities(self, value):
        return {"chat": {"available": True, "reason": "available"},
                "webSearch": {"available": False, "reason": "disabled"},
                "tmdb": {"available": True, "reason": "available"}}

    def m_ai_chat(self, value):
        if self.fail_ai:
            from mt_plugin import SDKError
            raise SDKError("boom", self.fail_ai)
        return {"message": {"role": "assistant", "content": self.ai_reply},
                "usage": {}, "metrics": {}, "nativeWebSearch": {"performed": False, "sources": []}}

    def m_tmdb_search(self, value):
        return self.tmdb_search.get(value["query"], [])

    def m_tmdb_detail(self, value):
        return self.tmdb_detail[(value["id"], value["mediaType"])]

    def m_tmdb_season(self, value):
        # 默认返回「查询不可用」，让季号校验降级跳过；专项用例再覆盖
        return []

    def m_organize_profiles(self, value):
        return [{"id": 7, "name": "电影与剧集", "kind": "organize", "revision": "1",
                 "enabled": True, "source": {"ref": "s", "name": "src", "readable": True,
                                             "writable": False, "deletable": False},
                 "destination": {"ref": "d", "name": "dst", "readable": True,
                                 "writable": True, "deletable": False}, "settings": {}}]

    def m_organize_manual_prepareBatch(self, value):
        items = []
        for index, item in enumerate(value["items"]):
            self.prepared.append(item)
            items.append({**item, "id": f"draft-{index}", "revision": "1"})
        return {"items": items}

    def m_organize_manual_run(self, value):
        self.queued.append(value)
        return {"ref": f"task-{len(self.queued)}", "runId": "t", "kind": "organize",
                "status": "queued", "total": 1, "completed": 0, "failed": 0, "updatedAt": ""}

    def m_files_move(self, value):
        if self.fail_move:
            from mt_plugin import SDKError
            raise SDKError("移动被拒绝", "MOVE_DENIED")
        self.moved.append(value)
        return {"ok": True}

    def m_tasks_wait(self, value):
        self.waiting.extend(value["refs"])
        return {"waiting": True}

    def m_tasks_list(self, value):
        """把已提交的整理任务按 task_status 里的设定回放成任务列表。"""
        items = []
        for index in range(len(self.queued)):
            ref = f"task-{index + 1}"
            status = self.task_status.get(ref, "queued")
            items.append({"ref": ref, "runId": "t", "kind": "organize", "status": status,
                          "total": 1, "completed": 1 if status == "done" else 0,
                          "failed": 1 if status == "failed" else 0, "updatedAt": ""})
        items.extend(self.extra_tasks)
        return {"items": items, "nextCursor": ""}

    def m_tasks_cancel(self, value):
        ref = value["ref"]
        if self.fail_cancel:
            from mt_plugin import SDKError
            raise SDKError("取消失败", "CANCEL_BOOM")
        self.cancelled.append(ref)
        self.task_status[ref] = "cancelled"
        return True

    def m_storage_set(self, value):
        self.kv[value["key"]] = {"found": True, "value": value["value"], "revision": "1", "bytes": 0}
        return self.kv[value["key"]]

    def m_storage_get(self, value):
        entry = self.kv.get(value["key"])
        if entry is None:
            return {"key": value["key"], "found": False, "bytes": 0, "revision": ""}
        return {"key": value["key"], **entry}

    def m_storage_batch(self, value):
        out = []
        for op in value["operations"]:
            if op["operation"] == "set":
                out.append(self.m_storage_set(op))
        return out

    def m_storage_list(self, value):
        prefix = value.get("prefix", "")
        keys = sorted(k for k in self.kv if k.startswith(prefix))
        items = [{"key": k, **self.kv[k]} for k in keys]
        return {"items": items, "nextCursor": ""}


def ctx_for(host: FakeHost, files, config=None) -> Context:
    data = {
        "pluginId": "ai.tmdb-organizer", "instanceId": "i", "runId": "run-1",
        "flowId": "flow-1", "source": "page", "packageHash": "h", "configRevision": "1",
        "config": config or {"profileId": 7, "mode": "draft", "batchSize": 8,
                             "yearTolerance": 1, "maxMinutes": 20, "nativeSearch": False},
        "files": files, "event": {}, "reason": "", "dataDir": "/tmp", "workDir": "/tmp",
    }
    return Context(data, host, "inv-1")


FILES = [
    {"ref": "f1", "name": "Dune.Part.Two.2024.2160p.WEB-DL.mkv", "path": "in/Dune.Part.Two.2024.2160p.WEB-DL.mkv",
     "rootId": "r", "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
    {"ref": "f2", "name": "Severance.S02E03.1080p.WEB-DL.mkv", "path": "in/Severance.S02E03.1080p.WEB-DL.mkv",
     "rootId": "r", "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
    {"ref": "f3", "name": "Severance.S02E04.1080p.WEB-DL.mkv", "path": "in/Severance.S02E04.1080p.WEB-DL.mkv",
     "rootId": "r", "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
    {"ref": "f4", "name": "readme.txt", "path": "in/readme.txt", "rootId": "r",
     "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""},
]

AI_REPLY = json.dumps({"items": [
    {"fileRef": "f1", "title": "沙丘2", "originalTitle": "Dune: Part Two", "year": 2024,
     "mediaType": "movie", "tmdbId": 693134, "season": None, "episode": None,
     "multiEpisode": False, "reason": "文件名含 Dune Part Two 2024", "sources": []},
    {"fileRef": "f2", "title": "人生切割术", "originalTitle": "Severance", "year": 2022,
     "mediaType": "tv", "tmdbId": 95396, "season": 2, "episode": 3,
     "multiEpisode": False, "reason": "", "sources": []},
    {"fileRef": "f3", "title": "人生切割术", "originalTitle": "Severance", "year": 2022,
     "mediaType": "tv", "tmdbId": 95396, "season": 2, "episode": 4,
     "multiEpisode": False, "reason": "", "sources": []},
    {"fileRef": "not-a-real-ref", "title": "伪造条目", "mediaType": "movie", "tmdbId": 1},
]}, ensure_ascii=False)

SEARCH = {
    "沙丘2": [{"id": 693134, "title": "沙丘2", "originalTitle": "Dune: Part Two",
              "year": "2024", "mediaType": "movie", "overview": ""}],
    "人生切割术": [{"id": 95396, "title": "人生切割术", "originalTitle": "Severance",
                 "year": "2022", "mediaType": "tv", "overview": ""}],
    # AI 不可用时的本地兜底：用文件名解析出的英文名去搜
    "Dune Part Two": [{"id": 693134, "title": "沙丘2", "originalTitle": "Dune: Part Two",
                       "year": "2024", "mediaType": "movie", "overview": ""}],
    "Severance": [{"id": 95396, "title": "人生切割术", "originalTitle": "Severance",
                   "year": "2022", "mediaType": "tv", "overview": ""}],
}
DETAIL = {
    (693134, "movie"): {"id": 693134, "title": "沙丘2", "originalTitle": "Dune: Part Two",
                        "year": "2024", "mediaType": "movie", "overview": ""},
    (95396, "tv"): {"id": 95396, "title": "人生切割术", "originalTitle": "Severance",
                    "year": "2022", "mediaType": "tv", "overview": ""},
}


def run_case(title, expect, mutate=None, config=None, fail_ai=None):
    host = FakeHost(FILES, AI_REPLY, SEARCH, DETAIL, fail_ai=fail_ai)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    if mutate:
        mutate(host, handlers)
    ctx = ctx_for(host, FILES, config)
    action_input = {"profileId": (config or {}).get("profileId", 7),
                    "mode": (config or {}).get("mode", "draft")}
    result = handlers[("action", "start")](action_input, ctx)
    print(f"\n=== {title} ===")
    print("  返回:", json.dumps(result, ensure_ascii=False)[:220])
    print("  AI 调用:", sum(1 for n, _ in host.calls if n == "ai.chat"),
          "| TMDB 搜索:", sum(1 for n, _ in host.calls if n == "tmdb.search"),
          "| 草稿:", len(host.prepared), "| 入队:", len(host.queued))
    expect(host, result)
    return host


def case_draft(host, result):
    assert len(host.prepared) == 3, host.prepared
    assert all(item["profileId"] == 7 for item in host.prepared)
    tv = [i for i in host.prepared if i["mediaType"] == "tv"]
    assert {i["season"] for i in tv} == {2}
    assert sorted(i["episode"] for i in tv) == [3, 4]
    assert all(i["tmdbId"] in (693134, 95396) for i in host.prepared)
    assert result["drafts"] == 3


def case_direct(host, result):
    assert len(host.queued) == 3, host.queued
    keys = [item["idempotencyKey"] for item in host.queued]
    assert len(set(keys)) == 3
    assert host.waiting, "直接整理必须登记 tasks.wait"


def case_no_ai(host, result):
    """AI 不可用时：本地解析出名称，靠「唯一候选」兜底仍可整理。"""
    assert len(host.prepared) == 3, host.prepared
    # 每个 ID 都必须来自唯一候选匹配，不是凭空猜的
    ids = {item["tmdbId"] for item in host.prepared}
    assert ids == {693134, 95396}, ids
    print("    （AI 挂了，靠唯一候选匹配补上 ID）")


def case_year_conflict(host, result):
    # 识别年份比 TMDB 早 5 年，容差 1 → 不能整理
    assert not host.prepared and not host.queued
    assert any("年份" in json.dumps(i, ensure_ascii=False)
               for i in [r for r in host.kv.values()]), "应记录年份不一致"


def case_truncated(host, result):
    """截断的 JSON 不抢救局部结果；靠本地解析+唯一候选兜底，仍不采纳任何模型条目。"""
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    assert all(r["aiUsed"] is False for r in records), "截断回答不应被采纳"
    if host.prepared:
        assert all(item["tmdbId"] in (693134, 95396) for item in host.prepared)
    print("    （截断回答未采纳，兜底靠本地解析）")


if __name__ == "__main__":
    run_case("草稿审阅模式", case_draft)

    run_case("直接整理模式", case_direct,
             config={"profileId": 7, "mode": "direct", "batchSize": 8,
                     "yearTolerance": 1, "maxMinutes": 20})

    run_case("AI 不可用 → 本地解析", case_no_ai, fail_ai="AI_TIMEOUT")

    def shift_years(host, handlers):
        host.ai_reply = AI_REPLY.replace('"year": 2024', '"year": 2019').replace('"year": 2022', '"year": 2016')

    run_case("年份冲突", case_year_conflict, mutate=shift_years)

    def broken(host, handlers):
        host.ai_reply = '```json\n[{"ref": "f1", "title": "沙丘2", "mediaType": "movie", "year": 20'

    run_case("截断回答不抢救", case_truncated, mutate=broken)

    print("\n全部用例通过")
