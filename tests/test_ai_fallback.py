"""AI 调用降级：宿主没开联网搜索时，应改用普通调用而不是直接放弃。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import MANIFEST, FakeHost, ctx_for  # noqa: E402

FILES = [{"ref": "f1", "name": "Severance.S02E03.mkv", "path": "in/x.mkv", "rootId": "r",
          "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""}]

REPLY = json.dumps({"items": [{
    "fileRef": "f1", "title": "人生切割术", "originalTitle": "Severance", "year": 2022,
    "mediaType": "tv", "tmdbId": 95396, "season": 2, "episode": 3,
    "multiEpisode": False, "sources": []}]})

DETAIL = {(95396, "tv"): {"id": 95396, "title": "人生切割术", "originalTitle": "Severance",
                          "year": "2022", "mediaType": "tv", "overview": ""}}


class NoNativeHost(FakeHost):
    """模拟宿主没开 AI 联网：nativeWebSearch=True 时报 AI_NATIVE_SEARCH_DISABLED。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.chat_calls: list[dict] = []

    def m_ai_chat(self, value):
        self.chat_calls.append(dict(value))
        if value.get("nativeWebSearch"):
            from mt_plugin import SDKError
            raise SDKError("请在 MT 的 AI 设置中启用联网搜索", "AI_NATIVE_SEARCH_DISABLED")
        return {"message": {"role": "assistant", "content": REPLY},
                "usage": {}, "metrics": {},
                "nativeWebSearch": {"performed": False, "sources": []}}


class BrokenAIHost(FakeHost):
    """AI 完全不可用。"""

    def m_ai_chat(self, value):
        from mt_plugin import SDKError
        raise SDKError("模型超时", "AI_TIMEOUT")


def run(host, config=None):
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    cfg = {"nativeSearch": True, "batchSize": 8, "yearTolerance": 1, "maxMinutes": 20}
    cfg.update(config or {})
    ctx = ctx_for(host, FILES, cfg)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    records = [v["value"] for k, v in host.kv.items() if "/items/" in k]
    return host, records


def test_native_disabled_falls_back():
    host = NoNativeHost(FILES, REPLY, {}, DETAIL)
    host, records = run(host)
    # 第一次带 nativeWebSearch 失败，第二次普通调用成功
    assert len(host.chat_calls) == 2, host.chat_calls
    assert host.chat_calls[0].get("nativeWebSearch") is True
    assert not host.chat_calls[1].get("nativeWebSearch")
    assert records[0]["status"] == "submitted", records[0]
    assert records[0]["aiUsed"] is True, "降级后仍是 AI 识别"
    assert host.prepared and host.prepared[0]["tmdbId"] == 95396
    print("  联网未开: 降级为普通调用，识别成功 ✓")


def test_native_disabled_only_once():
    """降级后后续批次不再重复尝试原生搜索。"""
    files = [dict(FILES[0], ref="f1"), dict(FILES[0], ref="f2", name="Severance.S02E04.mkv")]
    reply = json.dumps({"items": [
        {"fileRef": "f1", "title": "人生切割术", "originalTitle": "Severance", "year": 2022,
         "mediaType": "tv", "tmdbId": 95396, "season": 2, "episode": 3,
         "multiEpisode": False, "sources": []},
        {"fileRef": "f2", "title": "人生切割术", "originalTitle": "Severance", "year": 2022,
         "mediaType": "tv", "tmdbId": 95396, "season": 2, "episode": 4,
         "multiEpisode": False, "sources": []},
    ]})
    host = NoNativeHost(files, reply, {}, DETAIL)
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, files, {"nativeSearch": True, "batchSize": 1,
                                "yearTolerance": 1, "maxMinutes": 20})
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    # 批次 1: native 失败 + 普通成功 = 2 次；批次 2: 直接普通 = 1 次
    assert len(host.chat_calls) == 3, [c.get("nativeWebSearch") for c in host.chat_calls]
    assert [bool(c.get("nativeWebSearch")) for c in host.chat_calls] == [True, False, False]
    print("  降级只发生一次: 后续批次不再试原生搜索 ✓")


def test_ai_broken_uses_local():
    host = BrokenAIHost(FILES, REPLY, {}, DETAIL)
    host, records = run(host)
    assert records[0]["aiUsed"] is False, records[0]
    assert "本地文件名解析" in records[0]["reason"], records[0]["reason"]
    assert not host.prepared
    print("  AI 完全不可用: 退回本地解析 ✓")


if __name__ == "__main__":
    test_native_disabled_falls_back()
    test_native_disabled_only_once()
    test_ai_broken_uses_local()
    print("\nAI 降级用例全部通过")
