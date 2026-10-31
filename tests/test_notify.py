"""通知功能：成功/失败都要发，且通知失败不能影响整理结果。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

import notify  # noqa: E402
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


class NotifyHost(FakeHost):
    def __init__(self, *a, fail=False, **kw):
        super().__init__(*a, **kw)
        self.sent: list[dict] = []
        self.fail = fail

    def m_notification_send(self, value):
        if self.fail:
            from mt_plugin import SDKError
            raise SDKError("通道不可用", "OPERATION_FAILED")
        self.sent.append(dict(value))
        return True

    def m_runtime_resources(self, value):
        return {"roots": [], "organizeProfileIds": [7], "strmProfileIds": [],
                "embyServerIds": [], "notificationChannelIds": ["ch_test"],
                "http": []}


def run(host, config=None):
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    # 这些用例验证「单条汇总」路径，显式关掉按剧集聚合
    cfg = {"batchSize": 8, "yearTolerance": 1, "maxMinutes": 20,
           "notifyBySeries": False}
    cfg.update(config or {})
    ctx = ctx_for(host, FILES, cfg)
    handlers[("action", "start")]({"profileId": 7, "mode": "draft"}, ctx)
    return host


def test_success_notifies():
    host = run(NotifyHost(FILES, REPLY, {}, DETAIL))
    assert host.sent, "成功时也要发通知"
    msg = host.sent[0]
    assert "成功" in msg["title"], msg["title"]
    assert "已提交整理：1" in msg["content"], msg["content"]
    print("  成功通知 ✓", msg["title"])


def test_failure_notifies():
    """全部识别失败时也要发通知，并列出失败文件。"""
    reply = json.dumps({"items": [{
        "fileRef": "f1", "title": "某剧", "originalTitle": "Show", "year": 2022,
        "mediaType": "tv", "tmdbId": None, "season": 2, "episode": 3,
        "multiEpisode": False, "sources": [], "reason": "无法确认条目"}]})
    host = run(NotifyHost(FILES, reply, {}, {}))
    assert host.sent, "失败时也要发通知"
    msg = host.sent[0]
    assert "未识别" in msg["content"] or "失败" in msg["content"], msg["content"]
    print("  失败通知 ✓", msg["title"])


def test_notify_disabled():
    host = run(NotifyHost(FILES, REPLY, {}, DETAIL), {"notifyEnabled": False})
    assert not host.sent, "关闭后不应发送"
    print("  关闭通知: 不发送 ✓")


def test_notify_failure_does_not_break():
    """通知通道故障不能影响整理结果。"""
    host = run(NotifyHost(FILES, REPLY, {}, DETAIL, fail=True))
    assert host.prepared, "通知失败不应影响草稿生成"
    print("  通知故障: 整理不受影响 ✓")


def test_channel_from_config():
    host = run(NotifyHost(FILES, REPLY, {}, DETAIL), {"notifyChannelId": "ch_custom"})
    assert host.sent[0]["channelId"] == "ch_custom", host.sent[0]
    print("  指定通道: 优先使用配置值 ✓")


def test_channel_fallback_to_granted():
    host = run(NotifyHost(FILES, REPLY, {}, DETAIL))
    assert host.sent[0]["channelId"] == "ch_test", host.sent[0]
    print("  留空: 回退到已授权通道 ✓")


def test_series_notify_one_per_show():
    """默认按剧集聚合：一部剧一条，不是一集一条。"""
    host = run(NotifyHost(FILES, REPLY, {}, DETAIL), {"notifyBySeries": True})
    assert len(host.sent) == 1, [m["title"] for m in host.sent]
    msg = host.sent[0]
    assert "入库" in msg["title"], msg["title"]
    assert "人生切割术" in msg["title"], msg["title"]
    assert "第 2 季" in msg["title"], msg["title"]
    print("  按剧集通知 ✓", msg["title"])


def test_summary_content():
    counts = {"total": 10, "matched": 7, "submitted": 7, "failed": 2,
              "unrecognized": 1, "identity_conflict": 0, "needs_review": 0}
    items = [
        {"name": "a.mkv", "status": "failed", "reason": "TMDB 详情读取失败"},
        {"name": "b.mkv", "status": "unrecognized", "reason": "模型没有给出可靠的 TMDB ID"},
    ]
    title, body = notify.build_summary(counts, "direct", "临时整理", items)
    assert "直接整理" in title and "成功 7" in title, title
    assert "方案：临时整理" in body
    assert "a.mkv" in body and "TMDB 详情读取失败" in body
    assert "❌ 失败：2" in body
    print("  汇总内容 ✓", title)


if __name__ == "__main__":
    test_success_notifies()
    test_series_notify_one_per_show()
    test_failure_notifies()
    test_notify_disabled()
    test_notify_failure_does_not_break()
    test_channel_from_config()
    test_channel_fallback_to_granted()
    test_summary_content()
    print("\n通知用例全部通过")
