"""自检：在宿主里跑一次「诊断」，把所有前置条件逐条打印出来。

用法：装好插件后，在页面点「运行诊断」，或直接运行 start 动作，
      插件会把结果写进日志与运行结果。这个文件不参与打包。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.12" / "site-packages"))

MANIFEST = json.loads((ROOT / "plugin.json").read_text("utf-8"))

# 本插件实际会调用的宿主方法 → 需要的权限
REQUIRED = {
    "ai.capabilities": "",
    "ai.chat": "ai.chat",
    "ai.web.search": "ai.web.search",
    "tmdb.search": "tmdb.read",
    "tmdb.detail": "tmdb.read",
    "organize.profiles": "organize.read",
    "organize.manual.prepareBatch": "organize.prepare",
    "organize.manual.run": "organize.manual.run",
    "files.scan": "files.read",
    "tasks.items": "tasks.read",
    "tasks.list": "tasks.read",
    "tasks.get": "tasks.read",
    "tasks.cancel": "tasks.cancel",
    "tasks.wait": "tasks.wait",
    "storage.get": "storage",
    "storage.set": "storage",
    "storage.batch": "storage",
    "storage.list": "storage",
    "runtime.info": "",
    "runtime.progress": "",
    "runtime.checkpoint": "",
    "runtime.result": "",
    "log": "",
}


def check_manifest() -> list[str]:
    """静态检查清单里可能被早期宿主拒绝的写法。"""
    problems: list[str] = []
    if MANIFEST.get("sdkVersion") != "2":
        problems.append("sdkVersion 不是字符串 \"2\"")
    if MANIFEST.get("entry") != "plugin.py:Plugin":
        problems.append("entry 必须是 plugin.py:Plugin")
    ui_block = MANIFEST.get("ui", {})
    if ui_block.get("version") != "2":
        problems.append("ui.version 不是字符串 \"2\"")
    declared = set(MANIFEST.get("permissions", []))
    for method, permission in REQUIRED.items():
        if permission and permission not in declared:
            problems.append(f"清单缺少权限 {permission}（{method} 需要）")
    # 只检查动作表单字段，不看清单顶层 description
    for action in MANIFEST.get("actions", []):
        for field in action.get("fields", []):
            extra = set(field) - {"key", "label", "type", "required", "default",
                                  "placeholder", "upgradeDefault"}
            if extra:
                problems.append(
                    f"动作 {action['id']} 的字段 {field.get('key')} 用了非标准属性 {sorted(extra)}，"
                    "早期宿主可能拒绝"
                )
    return problems


class ProbeHost:
    """只读探测：任何方法都记录调用，返回最小可用数据。"""

    def __init__(self):
        self.calls: list[str] = []
        self.errors: dict[str, str] = {}

    def call(self, invocation, name, value):
        self.calls.append(name)
        handler = getattr(self, "m_" + name.replace(".", "_"), None)
        if handler is None:
            self.errors[name] = "插件调用了未模拟的方法（说明代码路径超出预期）"
            raise AssertionError(name)
        return handler(value)

    def m_runtime_info(self, value):
        return {"sdkVersion": "2", "instanceId": "probe", "runId": "probe", "action": "probe",
                "pluginVersion": "probe", "scheduleId": "", "parentRunId": "",
                "limits": {"maxWorkflowFiles": 500}}

    def m_runtime_capabilities(self, value):
        return []

    def m_ai_capabilities(self, value):
        return {"chat": {"available": False, "reason": "not_configured"},
                "webSearch": {"available": False, "reason": "disabled"},
                "tmdb": {"available": True, "reason": "available"}}

    def m_organize_profiles(self, value):
        return []

    def m_storage_get(self, value):
        return {"key": value["key"], "found": False, "bytes": 0, "revision": ""}

    def m_log(self, value):
        return True


def resolve_registry() -> tuple[Path | None, list[str]]:
    """定位 SDK 提供的 ui-registry.json。

    优先级：环境变量 MT_UI_REGISTRY → mt_plugin 包所在目录 → 项目根目录。
    该文件属于 SDK 契约数据，不应复制进本仓库。
    """
    import os

    candidates: list[Path] = []
    override = os.environ.get("MT_UI_REGISTRY")
    if override:
        candidates.append(Path(override))
    try:
        import mt_plugin

        candidates.append(Path(mt_plugin.__file__).resolve().parent / "ui-registry.json")
    except Exception:  # noqa: BLE001
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "ui-registry.json")

    for candidate in candidates:
        if candidate.is_file():
            return candidate, [str(item) for item in candidates]
    return None, [str(item) for item in candidates]


def check_page(ctx) -> list[str]:
    """逐节点校验页面组件与事件属性，复现宿主的 UI v2 校验。"""
    import json as _json
    from pathlib import Path as _Path

    problems: list[str] = []
    registry_path, tried = resolve_registry()
    if registry_path is None:
        return [
            "找不到 ui-registry.json，页面属性校验**未执行**（不能视为通过）。已查找："
            + "、".join(tried)
            + "；可用环境变量 MT_UI_REGISTRY 指定"
        ]
    registry = _json.loads(registry_path.read_text("utf-8"))
    allowed = registry["components"]
    host_actions = set(registry.get("hostActions", []))
    event_props = {"type", "path", "value", "actionId", "queryId", "name", "input", "when"}

    def walk(node, path: str) -> None:
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        name = node.get("component")
        if name:
            spec = allowed.get(name)
            if spec is None:
                problems.append(f"{path}: 未知组件 {name}")
            else:
                for prop in (node.get("props") or {}):
                    if prop not in spec:
                        problems.append(f"{path}.{name}.props.{prop}: 不支持此属性")
        for key, value in node.items():
            if key == "props":
                for prop_key, prop_value in (value or {}).items():
                    walk(prop_value, f"{path}.{name}.props.{prop_key}")
            elif key == "children":
                walk(value, f"{path}.{name}.children")
            elif key == "columns":
                walk(value, f"{path}.{name}.columns")
            elif key == "onClick":
                for index, event in enumerate(value or []):
                    if not isinstance(event, dict):
                        continue
                    extra = set(event) - event_props
                    if extra:
                        problems.append(f"{path}.onClick[{index}]: 事件属性 {sorted(extra)} 不被支持")
                    if event.get("type") == "host":
                        if "name" not in event:
                            problems.append(f"{path}.onClick[{index}]: host 事件缺 name")
                        elif host_actions and event["name"] not in host_actions:
                            problems.append(f"{path}.onClick[{index}]: 未知 host action {event['name']}")

    try:
        page = ctx["plugin"].get_page(ctx["context"])
    except Exception as error:  # noqa: BLE001
        return [f"get_page 抛错：{type(error).__name__} {error}"]
    walk(page, "page")
    manifest_path = _Path(__file__).resolve().parent.parent / "plugin.json"
    problems.extend(check_invoke(page, _json.loads(manifest_path.read_text("utf-8"))))
    return problems


def check_invoke(page, manifest: dict) -> list[str]:
    """invoke 事件的参数必须满足动作 inputSchema 的必填字段。

    页面里 bind="input.xxx" 的字段会被宿主合并进动作参数；
    只写在 state 里的值不会，必然报「Schema 缺少必填字段」。
    """
    problems: list[str] = []
    bound: set[str] = set()

    def collect_binds(node) -> None:
        if isinstance(node, list):
            for item in node:
                collect_binds(item)
            return
        if not isinstance(node, dict):
            return
        bind = (node.get("props") or {}).get("bind")
        if isinstance(bind, str) and bind.startswith("input."):
            bound.add(bind[len("input."):])
        for value in node.values():
            collect_binds(value)

    collect_binds(page)
    actions = {item["id"]: item for item in manifest.get("actions", [])}

    def walk_events(node, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk_events(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        for index, event in enumerate((node.get("props") or {}).get("onClick") or []):
            if not isinstance(event, dict) or event.get("type") != "invoke":
                continue
            action_id = event.get("actionId")
            action = actions.get(action_id)
            if action is None:
                problems.append(f"{path}.onClick[{index}]: invoke 未知动作 {action_id}")
                continue
            required = set((action.get("inputSchema") or {}).get("required") or [])
            supplied = set((event.get("input") or {}).keys()) | bound
            missing = required - supplied
            if missing:
                problems.append(
                    f"{path}.onClick[{index}]: 动作 {action_id} 缺少必填字段 "
                    f"{sorted(missing)}——把它们 bind 到 input.* 或写进 invoke.input"
                )
        for key, value in node.items():
            walk_events(value, f"{path}.{key}")

    walk_events(page, "page")
    problems.extend(check_continuation_inputs(manifest))
    return problems


def check_continuation_inputs(manifest: dict) -> list[str]:
    """续接动作的必填字段必须由 tasks.wait 的 completion.input 提供。

    宿主不会自动填充续接参数；漏传就会报
    「input.xxx: Schema 缺少必填字段」，而且只在运行到那一步才暴露。
    """
    import re
    from pathlib import Path as _Path

    problems: list[str] = []
    source = (_Path(__file__).resolve().parent.parent / "plugin.py").read_text("utf-8")
    continuations = {a["id"]: a for a in manifest.get("actions", [])
                     if a.get("kind") == "continuation"}
    if not continuations:
        return problems

    # 抓出所有 tasks.wait(... completion={...}) 里的 action 与 input
    supplied: dict[str, set[str]] = {}
    pattern = re.compile(
        r"tasks\.wait\([^)]*?completion\s*=\s*\{(.*?)\}\s*\)", re.S)
    for match in pattern.finditer(source):
        block = match.group(1)
        action_match = re.search(r'"action"\s*:\s*"([^"]+)"', block)
        if not action_match:
            continue
        action_id = action_match.group(1)
        input_match = re.search(r'"input"\s*:\s*\{(.*?)\}', block, re.S)
        keys: set[str] = set()
        if input_match:
            keys = set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"\s*:', input_match.group(1)))
        supplied.setdefault(action_id, set()).update(keys)

    for action_id, action in continuations.items():
        required = set((action.get("inputSchema") or {}).get("required") or [])
        if not required:
            continue
        given = supplied.get(action_id)
        if given is None:
            problems.append(
                f"续接动作 {action_id} 需要 {sorted(required)}，"
                "但没有找到对应的 tasks.wait 调用"
            )
            continue
        missing = required - given
        if missing:
            problems.append(
                f"续接动作 {action_id} 需要 {sorted(missing)}，"
                "但 tasks.wait 的 completion.input 没提供"
            )
    return problems


def main() -> None:
    print("== 清单静态检查 ==")
    problems = check_manifest()
    if not problems:
        print("  通过")
    for item in problems:
        print("  -", item)

    print("\n== 宿主方法覆盖检查 ==")
    from mt_plugin.core import Context
    from mt_plugin.worker import load_plugin

    host = ProbeHost()
    plugin, handlers = load_plugin(ROOT, MANIFEST)
    ctx = Context({
        "pluginId": MANIFEST["id"], "instanceId": "probe", "runId": "probe", "flowId": "probe",
        "source": "cli", "packageHash": "probe", "configRevision": "1", "config": {},
        "files": [], "event": {}, "reason": "", "dataDir": "/tmp", "workDir": "/tmp",
    }, host, "probe")

    # 只跑只读通道：页面与查询，不触发业务
    from mt_plugin.core import current
    token = current.set(ctx)
    try:
        page = plugin.get_page(ctx)
        print("  get_page: OK,", len(page.get("content", [])), "个顶层组件")
    except Exception as error:  # noqa: BLE001
        print("  get_page: 失败 →", type(error).__name__, error)
        page = None
    finally:
        current.reset(token)

    print("\n== 页面属性校验（复现宿主 UI v2 校验） ==")
    registry_path, _ = resolve_registry()
    print("  注册表:", registry_path if registry_path else "未找到")
    if page is not None:
        ui_problems = check_page({"plugin": plugin, "context": ctx})
        if ui_problems:
            for item in ui_problems:
                print("  ✗", item)
            problems.extend(ui_problems)
        else:
            print("  全部合法 ✓")

    for kind, name in sorted(handlers):
        if kind != "query":
            continue
        try:
            handlers[(kind, name)]({}, ctx)
            print(f"  query {name}: OK")
        except Exception as error:  # noqa: BLE001
            print(f"  query {name}: 失败 →", type(error).__name__, error)

    print("\n== 声明的 handler ==")
    for kind, name in sorted(handlers):
        print(f"  {kind:12} {name}")

    print("\n== 结论 ==")
    if problems:
        print("  先修上面列出的清单问题；页面被拒通常是 ui 组件属性或 schema 写法过新。")
        raise SystemExit(1)
    print("  清单与只读通道没发现问题。若仍然「用不了」，把宿主日志里")
    print("  「插件加载 / 页面校验 / SDK 失败」三类记录贴出来，那里会有具体字段路径。")


if __name__ == "__main__":
    main()
