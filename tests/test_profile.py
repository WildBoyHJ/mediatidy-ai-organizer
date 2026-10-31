"""整理方案校验：每种失败都要写日志并给出可读原因。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

from mt_plugin.worker import load_plugin  # noqa: E402
from tests.simulate import MANIFEST, FakeHost, ctx_for  # noqa: E402

FILES = [{"ref": "f1", "name": "Dune.Part.Two.2024.mkv", "path": "in/x.mkv", "rootId": "r",
          "version": "v1", "size": 1, "isDir": False, "modifiedAt": ""}]


class ProfileHost(FakeHost):
    def __init__(self, profiles, *a, **kw):
        super().__init__(*a, **kw)
        self.profiles = profiles
        self.logs: list[str] = []

    def m_organize_profiles(self, value):
        return self.profiles

    def m_log(self, value):
        self.logs.append(f"{value.get('level')}: {value.get('message')}")
        return True


def profile(pid=110, name="临时整理", kind="organize", enabled=True,
            dest_writable=True, dest_path="/media/library",
            with_destination=True):
    item = {"id": pid, "name": name, "kind": kind, "revision": "1", "enabled": enabled,
            "source": {"ref": "s", "name": "src", "path": "/media/failed",
                       "readable": True, "writable": False, "deletable": False},
            "settings": {}}
    if with_destination:
        item["destination"] = {"ref": "d", "name": "dst", "path": dest_path,
                               "readable": True, "writable": dest_writable,
                               "deletable": False}
    return item


def run(profiles, profile_id=110):
    host = ProfileHost(profiles, FILES, json.dumps({"items": []}), {}, {})
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = ctx_for(host, FILES)
    try:
        handlers[("action", "start")]({"profileId": profile_id, "mode": "draft"}, ctx)
        error = None
    except Exception as exc:  # noqa: BLE001
        error = exc
    return host, error


def case_no_write_grant():
    """插件不申请 files.write，目标目录 writable 必为 false——这不是错误。"""
    host, error = run([profile(dest_writable=False)])
    assert error is None, f"不应因 writable=false 报错：{error}"
    assert any("使用方案" in m for m in host.logs), host.logs
    print("  目标目录 writable=false: 正常放行（写入由宿主校验）✓")


def case_missing_profile():
    host, error = run([profile(pid=51, name="示例方案")], profile_id=110)
    assert error is not None and getattr(error, "code", "") == "NO_PROFILE", error
    assert any("找不到整理方案 110" in m for m in host.logs), host.logs
    assert any("51 示例方案" in m for m in host.logs), "应列出可用方案"
    print("  方案不存在: 报错并列出可用方案 ✓")


def case_disabled():
    host, error = run([profile(enabled=False)])
    assert error is not None and getattr(error, "code", "") == "INVALID_PROFILE", error
    assert any("已停用" in m for m in host.logs), host.logs
    print("  方案已停用: 报错并写日志 ✓")


def case_no_destination_field():
    """宿主不返回 destination 字段时不能误判失败。"""
    host, error = run([profile(with_destination=False)])
    assert error is None, f"不应报错：{error}"
    assert any("使用方案" in m for m in host.logs), host.logs
    print("  destination 缺失: 不误判，继续执行 ✓")


def case_ok():
    host, error = run([profile()])
    assert error is None, error
    assert any("使用方案「临时整理」" in m and "/media/library" in m for m in host.logs), host.logs
    print("  正常方案: 记录源与目标路径 ✓")


if __name__ == "__main__":
    case_no_write_grant()
    case_missing_profile()
    case_disabled()
    case_no_destination_field()
    case_ok()
    print("\n方案校验用例全部通过")
