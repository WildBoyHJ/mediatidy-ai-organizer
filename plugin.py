"""AI 识别 → TMDB 核对 → 整理。

流程：选择文件/目录 →（必要时）后台扫描 → 分批 AI 识别文件名 → TMDB 搜索与身份核对
→ 同剧一致性检查 → 草稿审阅或直接整理 → 等待宿主任务 → 归档结果。

契约要点：
* 只有 action/continuation 能调用 AI 与整理能力，query 只读 storage。
* 每批一次模型请求，singleRequest 不自动重发；失败保留已完成结果并继续。
* AI 不可用时退回本地文件名解析，不把「没有模型」当成「没有文件」。
"""
from __future__ import annotations

import calendar
import time
from typing import Any

from mt_plugin import Plugin as BasePlugin, Context, SDKError, action, continuation, query, ui

import identify
import naming
import notify

DEFAULT_BATCH = 8
DEFAULT_TOLERANCE = 1
DEFAULT_MINUTES = 20
MAX_FILES = 500
AI_CHAT_LIMIT = 78
# 每批 8 个 × 78 次 = 624，理论够 500 个；但失败重试会消耗额度，
# 留出余量，默认每轮最多处理这么多文件。
SAFE_FILES = 400
TMDB_SEARCH_LIMIT = 500
# 任务状态的归类：终态失败 / 仍可取消
# 任务状态的归类：仍可取消 / 终态失败 / 已取消
CANCELABLE = ("submitting", "queued", "running", "waiting")
FAILED_STATES = ("failed", "interrupted", "outcome_unknown")
CANCELLED_STATE = "cancelled"
# tasks.cancel 单次调用上限
CANCEL_LIMIT = 500
TASK_PAGE_SIZE = 20
# 识别阶段超过这么久没有新进度，就认为上一轮已经中断（否则按钮会被永久禁用）
# 识别结果的状态筛选取值（顺序即页面下拉顺序）
RESULTS_FILTERS = ("all", "identified", "matched", "submitted", "organized",
                   "unrecognized", "identity_conflict", "needs_review", "failed")
STALE_MINUTES = 15


def _age_minutes(stamp: str) -> float:
    try:
        then = time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return 0.0
    return (time.time() - calendar.timegm(then)) / 60.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Plugin(BasePlugin):
    # ---------------------------------------------------------------- 生命周期

    def validate_config(self, config: dict, ctx: Context) -> None:
        profile = config.get("profileId")
        if profile is not None and (not isinstance(profile, int) or profile <= 0):
            raise SDKError("请选择一个整理方案", "INVALID_CONFIG", path="config.profileId")
        mode = config.get("mode")
        if mode not in (None, "", "draft", "direct"):
            raise SDKError("提交方式只能是草稿审阅或直接整理", "INVALID_CONFIG", path="config.mode")
        batch = config.get("batchSize")
        if batch is not None and not 1 <= int(batch) <= 10:
            raise SDKError("每批文件数需在 1-10 之间", "INVALID_CONFIG", path="config.batchSize")

    def on_init(self, ctx: Context) -> None:
        saved = ctx.mt.storage.get(key="settings")
        if not saved["found"]:
            ctx.mt.storage.set(key="settings", value={"schemaVersion": 1})
        # 进度落盘的节流标记：flow_id -> (阶段, 已完成)
        self._progress_marks: dict[str, tuple[str, int]] = {}
        ctx.mt.log(level="info", message="AI 识别整理已初始化")

    # ---------------------------------------------------------------- 页面

    def get_page(self, ctx: Context) -> dict:
        # 刷新页面会重置 input / state 两个可写空间，选择项会变空白。
        # 所以：① 选择项 bind 到 state；② state 默认值从 storage 里上次的选择读回来；
        # ③ 进度除了 run.* 再兜一层 storage，刷新后仍能看到上一轮的阶段与计数。
        saved = ctx.mt.storage.get(key="settings")
        settings = saved["value"] if saved["found"] else {}
        last_profile = settings.get("lastProfileId")
        if not isinstance(last_profile, int) or last_profile <= 0:
            last_profile = None
        last_mode = settings.get("lastMode")
        if last_mode not in ("draft", "direct"):
            last_mode = "draft"

        running = ui.expr("or",
                          ui.expr("eq", ui.ref("run.status"), "queued"),
                          ui.expr("eq", ui.ref("run.status"), "running"),
                          ui.ref("query.progress.data.active"))
        ready = ui.expr("and",
                        ui.expr("not", running),
                        ui.ref("state.profileId"),
                        ui.expr("gt", ui.ref("stats.selections"), 0))
        return ui.page(
            ui.Alert(
                text="文件名与路径会发送到你配置的 AI 服务；识别结果还要经过 TMDB 核对才会进入整理。",
                tone="muted",
            ),
            ui.Card(
                ui.Text(text="1. 选择整理方案与提交方式"),
                ui.ProfilePicker(label="整理方案", bind="state.profileId"),
                ui.Select(label="提交方式", bind="state.mode", options=[
                    {"label": "草稿审阅（推荐）", "value": "draft"},
                    {"label": "直接整理", "value": "direct"},
                ]),
                ui.Text(text="2. 选择要处理的视频文件或目录"),
                ui.FilePicker(),
                ui.Button(
                    label="开始识别与整理",
                    onClick=[{"type": "invoke", "actionId": "start",
                              "input": {"profileId": ui.ref("state.profileId"),
                                        "mode": ui.ref("state.mode")}}],
                    disabled=ui.expr("not", ready),
                ),
                ui.Text(text=ui.expr(
                    "if",
                    running,
                    "上一轮还在进行中，完成或取消后才能开始新的一轮",
                    ui.expr("if",
                            ui.expr("eq", ui.ref("stats.selections"), 0),
                            "请先选择文件或目录",
                            ui.expr("if", ui.ref("state.profileId"),
                                    "准备就绪，可以开始", "请选择整理方案")),
                ), tone="muted", size="sm"),
                title="开始",
            ),
            ui.Card(
                ui.Steps(
                    labels=ui.ref("query.progress.data.stepLabels"),
                    value=ui.ref("query.progress.data.stepIndex"),
                ),
                ui.Progress(
                    label=ui.ref("query.progress.data.message"),
                    value=ui.ref("query.progress.data.completed"),
                    total=ui.ref("query.progress.data.total"),
                ),
                ui.Table(
                    source=ui.ref("query.progress.data.steps"),
                    rowKey="label",
                    pageSize=10,
                    columns=[
                        {"label": "步骤", "field": "label", "cells": []},
                        {"label": "执行方", "field": "owner", "cells": []},
                        {"label": "状态", "field": "state", "cells": []},
                        {"label": "进度", "field": "detail", "cells": []},
                    ],
                ),
                ui.Stack(
                    ui.Button(label="刷新进度", variant="outline", onClick=[
                        {"type": "query", "queryId": "progress"},
                        {"type": "query", "queryId": "tasks"},
                        {"type": "query", "queryId": "results"},
                    ]),
                    direction="row", gap="sm",
                ),
                ui.Text(
                    text=ui.expr(
                        "if",
                        running,
                        "扫描与 MT 整理两行的进度是现查任务状态得到的；宿主只在整轮结束后自动刷新，"
                        "运行中点「刷新进度」更新。",
                        ui.ref("query.results.data.message"),
                    ),
                    tone="muted",
                    size="sm",
                ),
                title="流程",
            ),
            ui.Card(
                ui.Text(text=ui.ref("query.tasks.data.message"), size="sm"),
                ui.Text(
                    text=ui.ref("query.tasks.data.current"),
                    tone="primary",
                    size="sm",
                ),
                ui.Table(
                    source=ui.ref("query.tasks.data.items"),
                    rowKey="ref",
                    pageSize=20,
                    columns=[
                        {"label": "作品", "field": "label", "cells": []},
                        {"label": "状态", "field": "statusLabel", "cells": []},
                        {"label": "操作", "cells": [ui.Button(
                            label="取消",
                            variant="outline",
                            onClick=[{"type": "invoke", "actionId": "cancel-task",
                                      "input": {"ref": ui.ref("row.ref")}},
                                     {"type": "query", "queryId": "tasks"},
                                     {"type": "query", "queryId": "results"}],
                        )]},
                    ],
                ),
                ui.Stack(
                    ui.Button(label="刷新任务列表", variant="outline", onClick=[
                        {"type": "set", "path": "state.tasksCursor", "value": ""},
                        {"type": "query", "queryId": "tasks"},
                    ]),
                    ui.Button(
                        label="下一页",
                        variant="outline",
                        disabled=ui.expr("not", ui.ref("query.tasks.data.nextCursor")),
                        onClick=[{"type": "set", "path": "state.tasksCursor",
                                  "value": ui.ref("query.tasks.data.nextCursor")}],
                    ),
                    ui.Button(label="取消全部未完成任务", variant="destructive", onClick=[
                        {"type": "invoke", "actionId": "cancel-all", "input": {}},
                    ]),
                    direction="row", gap="sm",
                ),
                ui.Text(
                    text="只列进行中与等待中的任务；已完成、失败、已取消的刷新后不再显示。取消不可撤销。",
                    tone="warning",
                    size="sm",
                ),
                title=ui.expr(
                    "if",
                    ui.expr("gt", ui.expr("coalesce", ui.ref("query.tasks.data.total"), 0), 0),
                    "整理任务 · 待处理",
                    "整理任务",
                ),
            ),
            ui.Card(
                ui.Stack(
                    ui.Select(label="按状态筛选", bind="state.resultFilter", options=[
                        {"label": "全部", "value": "all"},
                        {"label": "已识别（待核对）", "value": "identified"},
                        {"label": "已核对（可整理）", "value": "matched"},
                        {"label": "已提交", "value": "submitted"},
                        {"label": "已完成", "value": "organized"},
                        {"label": "未识别", "value": "unrecognized"},
                        {"label": "身份冲突", "value": "identity_conflict"},
                        {"label": "需人工处理", "value": "needs_review"},
                        {"label": "失败", "value": "failed"},
                    ]),
                    ui.Button(label="应用筛选", variant="outline", onClick=[
                        {"type": "set", "path": "state.cursor", "value": ""},
                        {"type": "query", "queryId": "results"},
                    ]),
                    direction="row", gap="sm",
                ),
                ui.Table(
                    source=ui.ref("query.results.data.items"),
                    rowKey="id",
                    pageSize=20,
                    searchable=True,
                    columns=[
                        {"label": "文件", "field": "name", "cells": []},
                        {"label": "识别作品", "field": "title", "cells": []},
                        {"label": "TMDB", "field": "tmdb", "cells": []},
                        {"label": "季集", "field": "episodeLabel", "cells": []},
                        {"label": "状态", "field": "statusLabel", "cells": []},
                        {"label": "说明", "field": "reason", "cells": []},
                        {"label": "AI 原始回答", "field": "aiRaw", "cells": []},
                    ],
                ),
                ui.Stack(
                    ui.Button(label="刷新", variant="outline", onClick=[
                        {"type": "set", "path": "state.cursor", "value": ""},
                        {"type": "query", "queryId": "results"},
                    ]),
                    ui.Button(label="下一页", variant="outline",
                              disabled=ui.expr("not", ui.ref("query.results.data.nextCursor")),
                              onClick=[{"type": "set", "path": "state.cursor",
                                        "value": ui.ref("query.results.data.nextCursor")}]),
                    ui.Button(label="查看草稿", variant="outline",
                              onClick=[{"type": "host", "name": "reviewDrafts"}]),
                    direction="row", gap="sm",
                ),
                title="识别结果",
            ),
            ui.Card(ui.RunHistory(), title="运行记录"),
            state={"cursor": "", "mode": last_mode, "profileId": last_profile,
                   "tasksCursor": "", "resultFilter": "all"},
            queries=[{"id": "results", "name": "results",
                      "input": {"cursor": ui.ref("state.cursor"),
                                "status": ui.ref("state.resultFilter")}},
                     {"id": "tasks", "name": "tasks",
                      "input": {"cursor": ui.ref("state.tasksCursor")}},
                     {"id": "progress", "name": "progress", "input": {}}],
        )

    # ---------------------------------------------------------------- 入口动作

    @action("start")
    def start(self, input: dict, ctx: Context) -> dict:
        profile_id = self._profile_id(input, ctx)
        mode = self._mode(input, ctx)
        files, dirs = self._selected(ctx)
        if not files and not dirs:
            raise SDKError("请先选择视频文件或包含视频的目录", "NO_FILES")

        # 续接会换 run_id，但根流程 flow_id 不变；跨调用状态一律挂在 flow_id 上。
        flow_key = ctx.flow_id
        options = self._options(ctx)
        # 方案与选项必须落盘：续接时的 input 由宿主回填，不能当成可信参数来源。
        self._save_flow(ctx, flow_key, {
            "profileId": profile_id,
            "mode": mode,
            "options": options,
            "scanRefs": [],
            "stage": "scan" if dirs else "identify",
        })
        self._write_summary(ctx, flow_key, {
            "status": "scanning" if dirs else "identifying",
            "profileId": profile_id,
            "mode": mode,
            "counts": {"total": len(files), "matched": 0, "failed": 0, "submitted": 0},
            "message": "已开始",
            "createdAt": _now(),
        })
        ctx.mt.storage.set(key="currentRun", value=flow_key)
        # 记住这次的选择：刷新页面时 state 会重置，用它把方案与提交方式带回来
        saved = ctx.mt.storage.get(key="settings")
        settings = dict(saved["value"]) if saved["found"] else {"schemaVersion": 1}
        settings["lastProfileId"] = profile_id
        settings["lastMode"] = mode
        ctx.mt.storage.set(key="settings", value=settings)

        if not dirs:
            return self._pipeline(ctx, flow_key, files, profile_id, mode, options)

        refs = []
        for index, folder in enumerate(dirs):
            task = ctx.mt.files.scan(
                root_id=folder["rootId"],
                path=folder["path"],
                video_only=True,
                limit=SAFE_FILES,
                idempotency_key=f"{ctx.flow_id}:scan:{index}",
            )
            refs.append(task["ref"])
        self._patch_flow(ctx, flow_key, {"scanRefs": refs, "scanParent": ctx.run_id})
        self._save_pending(ctx, flow_key, files)
        self._report(ctx, "扫描目录", 0, 0, "扫描完成后自动开始识别")
        ctx.mt.log(level="info", message=f"已提交 {len(refs)} 个扫描任务，等待完成后继续识别")
        ctx.mt.tasks.wait(refs=refs, completion={
            "action": "after-scan",
            "input": {"flowKey": flow_key},
        })
        return {"waiting": True, "flowKey": flow_key, "scans": len(refs)}

    @continuation("after-scan")
    def after_scan(self, input: dict, ctx: Context) -> dict:
        flow_key = ctx.flow_id
        flow = self._load_flow(ctx, flow_key)
        if not flow:
            raise SDKError("找不到本次运行的流程记录，请重新开始", "FLOW_MISSING")
        profile_id = int(flow.get("profileId") or 0)
        requested = flow.get("mode")
        mode = "direct" if requested == "direct" else "draft"
        options = flow.get("options") or self._options(ctx)

        # 续接事件必须与原流程登记的扫描任务一致，避免串到别的运行。
        tasks = (ctx.event or {}).get("tasks") or []
        expected = set(flow.get("scanRefs") or [])
        received = {task.get("ref") for task in tasks}
        if expected and received != expected:
            raise SDKError("续接事件与本次扫描任务不匹配", "FLOW_MISMATCH")

        files = self._load_pending(ctx, flow_key)
        truncated = 0
        failed_scans = 0
        partial_scans = 0
        capped = 0
        for task in tasks:
            # 扫描可能因为达到容量上限而 failed，但已经产出的条目仍然可读。
            # 先取结果再判断成败，不要把扫到的文件丢掉。
            status = task.get("status")
            offset = 0
            while True:
                page = ctx.mt.tasks.items(ref=task["ref"], offset=offset, limit=100)
                for item in page.get("items", []):
                    record = item.get("file")
                    if record and not record.get("isDir"):
                        files.append(record)
                following = page.get("nextOffset") or 0
                if not following or following <= offset:
                    break
                offset = following
            if status != "done":
                completed = int(task.get("completed") or 0)
                if completed >= SAFE_FILES:
                    capped += 1
                    ctx.mt.log(level="warn",
                               message=f"目录里的视频超过单轮上限 {SAFE_FILES} 个，已用扫描到的 {completed} 个；"
                                       f"剩余文件请分批重新选择")
                elif files:
                    partial_scans += 1
                    ctx.mt.log(level="warn",
                               message=f"扫描任务 {task.get('ref', '')} 状态 {status}，使用已扫描到的部分结果")
                else:
                    failed_scans += 1
                    ctx.mt.log(level="warn", message=f"扫描任务 {task.get('ref', '')} 状态 {status}，无可用结果")

        deduped: list[dict] = []
        seen: set[str] = set()
        for record in files:
            if record["ref"] in seen:
                continue
            seen.add(record["ref"])
            if not naming.is_video(record.get("name", "")):
                continue
            deduped.append(record)
        if len(deduped) > MAX_FILES:
            truncated = len(deduped) - MAX_FILES
            deduped = deduped[:MAX_FILES]

        if failed_scans:
            ctx.mt.log(level="warn", message=f"{failed_scans} 个扫描任务未成功，结果可能不完整")
        if truncated:
            ctx.mt.log(level="warn", message=f"超过单轮上限，已截断 {truncated} 个文件")
        if not deduped:
            self._patch_summary(ctx, flow_key, {"status": "done", "message": "扫描没有找到视频文件"})
            return {"count": 0, "message": "扫描没有找到视频文件"}

        return self._pipeline(ctx, flow_key, deduped, profile_id, mode, options)

    # ---------------------------------------------------------------- 主流程

    def _pipeline(self, ctx: Context, flow_key: str, files: list[dict],
                  profile_id: int, mode: str, options: dict) -> dict:
        profile = self._check_profile(ctx, profile_id)
        info = ctx.mt.runtime.info()
        limits = info.get("limits") or {}
        max_files = int(limits.get("maxWorkflowFiles") or 100)
        cap = min(max_files, SAFE_FILES)
        if len(files) > cap:
            ctx.mt.log(level="warn",
                       message=f"文件数 {len(files)} 超过本轮上限 {cap}（宿主 {max_files}、AI 额度安全线 {SAFE_FILES}），"
                               f"只处理前 {cap} 个；其余请分批重新选择")
            files = files[:cap]

        deadline = time.monotonic() + options["minutes"] * 60
        batch_size = options["batch"]
        state: dict = {
            "aiCalls": 0,
            "tmdbSearches": 0,
            "tmdbDetails": 0,
            "aiUnavailable": "",
            "truncatedPreview": 0,
        }

        capabilities = self._capabilities(ctx)
        ai_ok = capabilities.get("chat", {}).get("available") is True
        if not ai_ok:
            state["aiUnavailable"] = capabilities.get("chat", {}).get("reason", "unavailable")
            ctx.mt.log(level="warn",
                       message=f"AI 不可用（{state['aiUnavailable']}），改用本地文件名解析")

        results: list[dict] = []
        total = len(files)
        self._report(ctx, "准备识别", 0, total, f"共 {total} 个文件")
        # 标记进行中，供页面刷新后用 progress 查询判断「运行中」；顺带记下识别总数
        self._patch_summary(ctx, flow_key,
                            {"status": "running", "message": "识别中",
                             "counts": {"total": total}})

        for start in range(0, len(files), batch_size):
            if time.monotonic() > deadline:
                for record in files[start:]:
                    results.append(self._record(record, status="failed", reason="超出识别时间上限，未处理"))
                ctx.mt.log(level="warn", message="达到识别时间上限，剩余文件未识别")
                break
            ctx.mt.runtime.checkpoint()
            chunk = files[start:start + batch_size]
            head = chunk[0].get("name", "") if chunk else ""
            tail = f" 等 {len(chunk)} 个" if len(chunk) > 1 else ""
            self._report(ctx, "识别中", len(results), total, f"正在处理 {head}{tail}")
            if ai_ok and int(state["aiCalls"]) < AI_CHAT_LIMIT:
                records, state = self._identify_batch(ctx, chunk, options, state)
            else:
                records = [self._local_record(item) for item in chunk]
            results.extend(records)
            # 把本批已确定的同剧身份喂给下一批，避免后续批次换到另一个 TMDB 条目
            state["seriesHint"] = identify.series_hint(results)
            done = min(start + len(chunk), total)
            self._report(ctx, "已识别", done, total,
                         chunk[-1].get("name", "") if chunk else "")
            self._persist(ctx, flow_key, results)
            ctx.mt.runtime.result(value={"counts": self._counts(results), "message": "识别中"})

        # TMDB 核对
        results, state = self._verify_all(ctx, results, options, state, deadline)
        self._persist(ctx, flow_key, results)

        # 同剧一致性
        results = self._check_series(results)
        self._persist(ctx, flow_key, results)

        counts = self._counts(results)
        ready = [item for item in results if item["status"] == "matched"]
        summary = {
            "status": "submitting" if ready else "done",
            "profileId": profile_id,
            "mode": mode,
            "counts": counts,
            "aiUnavailable": state["aiUnavailable"],
            "message": f"识别完成：可整理 {len(ready)}，失败 {counts['failed']}",
            "profile": profile.get("name", ""),
        }

        if not ready:
            self._patch_summary(ctx, flow_key, summary)
            ctx.mt.log(level="info", message="没有可整理的文件，保留识别结果")
            self._notify_result(ctx, counts, mode, profile.get("name", ""), results)
            return {"counts": counts, "message": summary["message"]}

        if mode == "draft":
            drafted = self._submit_drafts(ctx, flow_key, ready, profile_id)
            self._report(ctx, "草稿已生成", drafted, len(ready), f"{drafted} 条待确认")
            counts = self._counts(results)
            summary["counts"] = counts
            summary["status"] = "done"
            summary["message"] = f"已生成 {drafted} 条整理草稿，请在草稿页核对后确认"
            self._patch_summary(ctx, flow_key, summary)
            self._notify_result(ctx, counts, mode, profile.get("name", ""), results)
            ctx.mt.runtime.result(value=summary)
            return {"counts": counts, "drafts": drafted, "message": summary["message"]}

        refs = self._submit_direct(ctx, flow_key, ready, profile_id)
        self._report(ctx, "已提交整理", len(refs), len(refs), f"{len(refs)} 个任务已入队")
        counts = self._counts(results)
        summary["counts"] = counts
        if not refs:
            summary["status"] = "done"
            summary["message"] = "整理任务提交失败，请查看日志"
            self._patch_summary(ctx, flow_key, summary)
            self._notify_result(ctx, counts, mode, profile.get("name", ""), results)
            return {"counts": counts, "message": summary["message"]}
        summary["status"] = "waiting"
        summary["message"] = f"已提交 {len(refs)} 个整理任务，等待完成"
        self._patch_summary(ctx, flow_key, summary)
        self._persist(ctx, flow_key, results)
        ctx.mt.log(level="info", message=summary["message"])
        ctx.mt.tasks.wait(refs=refs, completion={"action": "collect", "input": {"flowKey": flow_key}})
        return {"counts": counts, "waiting": True, "tasks": len(refs)}

    # ---------------------------------------------------------------- 识别

    def _identify_batch(self, ctx: Context, chunk: list[dict],
                        options: dict, state: dict) -> tuple[list[dict], dict]:
        valid = {item["ref"] for item in chunk}
        messages = identify.build_messages(chunk, options["extra"], state.get("seriesHint", ""))
        started = time.monotonic()

        # 先按用户设置尝试。原生联网搜索需要宿主开启 AI 联网；宿主没开时
        # 会自动降级为普通调用，因为「没有联网」不等于「不能用 AI」。
        want_native = bool(options["nativeSearch"]) and not state.get("nativeDisabled")
        response = None
        try:
            response = ctx.mt.ai.chat(
                messages=messages,
                single_request=True,
                native_web_search=want_native,
            )
        except SDKError as error:
            state["aiCalls"] += 1
            if want_native and error.code in ("AI_NATIVE_SEARCH_DISABLED",
                                              "AI_NATIVE_SEARCH_NOT_PERFORMED",
                                              "AI_REQUEST_REJECTED"):
                state["nativeDisabled"] = error.code
                ctx.mt.log(level="warn",
                           message=f"宿主未启用 AI 联网搜索（{error.code}），"
                                   "本次起改用普通 AI 调用；识别仍会继续，但缺少网页交叉验证")
                try:
                    response = ctx.mt.ai.chat(messages=messages, single_request=True)
                except SDKError as retry_error:
                    state["aiCalls"] += 1
                    ctx.mt.log(level="warn",
                               message=f"AI 调用失败 {retry_error.code}，改用本地文件名解析")
                    state["aiUnavailable"] = retry_error.code
                    return [self._local_record(item) for item in chunk], state
                state["aiCalls"] += 1
            else:
                ctx.mt.log(level="warn", message=f"AI 批次失败 {error.code}，改用本地解析")
                if error.code in ("AI_NATIVE_SEARCH_DISABLED", "AI_NATIVE_SEARCH_NOT_PERFORMED",
                                  "AI_REQUEST_REJECTED", "AI_TIMEOUT", "AI_PROTOCOL_ERROR"):
                    state["aiUnavailable"] = error.code
                return [self._local_record(item) for item in chunk], state
        else:
            state["aiCalls"] += 1
        elapsed = int((time.monotonic() - started) * 1000)

        message = response.get("message") or {}
        raw_content = message.get("content") if isinstance(message, dict) else None
        text = raw_content if isinstance(raw_content, str) else ""
        entries, diagnostics = identify.parse_response(text, valid)
        shown = identify.preview(text)
        if shown["truncated"]:
            state["truncatedPreview"] += 1

        by_ref = {entry["ref"]: entry for entry in entries}
        records: list[dict] = []
        for item in chunk:
            entry = by_ref.get(item["ref"])
            if entry is None:
                fallback = self._local_record(item)
                fallback["reason"] = "模型未返回该文件，已用本地解析"
                fallback["preview"] = shown
                records.append(fallback)
                continue
            record = self._record(item, status="identified")
            record.update({
                "title": entry["title"],
                "originalTitle": entry["originalTitle"],
                "year": entry["year"],
                "mediaType": entry["mediaType"],
                "tmdbId": entry["tmdbId"],
                "season": entry["season"],
                "episode": entry["episode"],
                "multiEpisode": entry["multiEpisode"],
                "confidence": entry["confidence"],
                "sources": entry["sources"],
                "aiUsed": True,
                "modelMs": elapsed,
                "reason": entry["reason"],
            })
            if not record["tmdbId"]:
                hinted = naming.tmdb_id_from_path(item.get("path", ""), item.get("name", ""))
                if hinted:
                    record["tmdbId"] = hinted
                    note = f"使用目录标注的 TMDB ID {hinted}"
                    record["reason"] = f"{record['reason']}；{note}" if record["reason"] else note
            if not record["title"]:
                record["status"] = "unrecognized"
                record["reason"] = "模型没有给出作品名"
            record["preview"] = shown
            records.append(record)

        if diagnostics.get("error"):
            ctx.mt.log(level="warn", message=f"解析失败：{diagnostics['error']}")
        if diagnostics.get("unknownRefs"):
            ctx.mt.log(level="warn", message=f"丢弃 {diagnostics['unknownRefs']} 条陌生引用")
        if diagnostics.get("noTmdbId"):
            # 把模型原始输出打出来（截断），否则无法判断是模型不听话还是解析出错
            sample = identify.sanitize(text, 400) if text else "（空回答）"
            ctx.mt.log(level="warn",
                       message=f"{diagnostics['noTmdbId']} 个文件模型未给出 TMDB ID；"
                               f"原始回答片段：{sample}")
        if diagnostics.get("missingIds"):
            ctx.mt.log(level="warn", message=f"{diagnostics['missingIds']} 个文件模型未返回条目")
        native_raw: Any = response.get("nativeWebSearch")
        sources = (native_raw.get("sources") or []) if isinstance(native_raw, dict) else []
        if sources:
            ctx.mt.log(level="debug", message=f"本轮搜索来源 {len(sources)} 条")
        return records, state

    def _local_record(self, item: dict) -> dict:
        parsed = naming.parse(item.get("name", ""))
        record = self._record(item, status="identified")
        record.update({
            "title": parsed["title"],
            "originalTitle": "",
            "year": parsed["year"],
            "mediaType": parsed["mediaType"],
            "season": parsed["season"],
            "episode": parsed["episode"],
            "confidence": 0.0,
            "aiUsed": False,
            "reason": "本地文件名解析",
        })
        # 目录名里的 tmdb-xxxx 是明确标注，不用 AI 猜
        hinted = naming.tmdb_id_from_path(item.get("path", ""), item.get("name", ""))
        if hinted:
            record["tmdbId"] = hinted
            record["reason"] = f"使用目录标注的 TMDB ID {hinted}"
        if not record["title"]:
            record["status"] = "unrecognized"
            record["reason"] = "文件名无法解析出作品名"
        return record

    # ---------------------------------------------------------------- TMDB 核对

    def _verify_all(self, ctx: Context, results: list[dict], options: dict,
                    state: dict, deadline: float) -> tuple[list[dict], dict]:
        """核对 AI 给出的 tmdbId。插件不替模型挑候选——那是同名误判的主要来源。"""
        details: dict[tuple[int, str], dict | None] = {}
        seasons_cache: dict[int, list[int]] = {}
        tolerance = options["tolerance"]

        total = len(results)
        for index, record in enumerate(results):
            if record["status"] != "identified":
                continue
            if time.monotonic() > deadline:
                record["status"] = "failed"
                record["reason"] = "超出识别时间上限，未核对 TMDB"
                continue
            ctx.mt.runtime.checkpoint()
            self._report(ctx, "核对 TMDB", index + 1, total, record.get("name", ""))

            media_type = record.get("mediaType")
            tmdb_id = int(record.get("tmdbId") or 0)

            # 有 ID 但类型未知：用 TMDB 反查类型，而不是直接放弃。
            if media_type not in ("movie", "tv") and tmdb_id > 0:
                media_type = self._resolve_media_type(ctx, tmdb_id, details, state)
                if media_type:
                    record["mediaType"] = media_type
            if media_type not in ("movie", "tv"):
                # 保留模型给出的具体原因（如「避免猜测」），不要用通用文案覆盖
                record["status"] = "unrecognized"
                record["reason"] = record.get("reason") or (
                    "无法判断是电影还是剧集"
                    if tmdb_id <= 0 else "TMDB 上找不到该 ID 对应的电影或剧集"
                )
                record["tmdbId"] = tmdb_id if tmdb_id > 0 else 0
                continue
            if tmdb_id <= 0:
                # 模型没给 ID 时，用名称+年份做一次受控搜索：
                # 只有「唯一一个候选通过名称与年份校验」才采纳，多个候选一律不猜。
                resolved, why = self._resolve_unique(
                    ctx, record, details, state, tolerance)
                if resolved:
                    tmdb_id = resolved
                    record["tmdbId"] = tmdb_id
                    record["reason"] = f"{record.get('reason') or ''}；按名称与年份唯一匹配到 TMDB {tmdb_id}".strip("；")
                else:
                    # 保留识别阶段的原因（如「本地文件名解析」），追加兜底搜索的结论
                    base = record.get("reason") or ""
                    record["status"] = "unrecognized"
                    record["reason"] = f"{base}；{why}" if base and why else (base or why or "模型没有给出可靠的 TMDB ID")
                    continue

            identity = (tmdb_id, media_type)
            if identity not in details:
                if state["tmdbDetails"] >= TMDB_SEARCH_LIMIT:
                    record["status"] = "failed"
                    record["reason"] = "TMDB 查询次数用尽"
                    continue
                try:
                    details[identity] = ctx.mt.tmdb.detail(id=tmdb_id, media_type=media_type)
                except SDKError as error:
                    if error.code == "CANCELLED":
                        raise
                    ctx.mt.log(level="warn", message=f"TMDB 详情失败 {error.code}")
                    details[identity] = None
                state["tmdbDetails"] += 1

            detail = details[identity]
            if not detail:
                record["status"] = "failed"
                record["reason"] = "TMDB 详情读取失败，未生成整理请求"
                continue

            # 1) ID 与类型必须与返回一致
            if int(detail.get("id") or 0) != tmdb_id or detail.get("mediaType") != media_type:
                record["status"] = "identity_conflict"
                record["reason"] = "TMDB 返回的 ID 或类型与识别结论不一致"
                continue

            # 2) 名称必须有交集；模型给的别名可能对不上，但 TMDB 的标题应至少命中其一
            if not identify.title_matches(
                [detail.get("title", ""), detail.get("originalTitle", "")],
                [record.get("title", ""), record.get("originalTitle", "")],
            ):
                record["status"] = "identity_conflict"
                record["reason"] = (
                    f"识别为「{record.get('title') or '未知'}」，但 TMDB ID {tmdb_id} "
                    f"对应「{detail.get('title') or detail.get('originalTitle') or '未知作品'}」"
                )
                record["tmdbId"] = tmdb_id
                continue

            # 3) 年份容差；模型给的年份为 0 时以 TMDB 为准
            year = self._year(detail.get("year", ""))
            if record["year"] and year and abs(record["year"] - year) > tolerance:
                record["status"] = "identity_conflict"
                record["reason"] = f"年份不一致（识别 {record['year']}，TMDB {year}）"
                record["tmdbId"] = tmdb_id
                continue

            # 4) 季集合法性：用 TMDB 真实季列表核对，而不是只看有没有数字
            if media_type == "tv":
                record["tmdbId"] = tmdb_id
                if record.get("multiEpisode"):
                    record["status"] = "needs_review"
                    record["reason"] = "多集合集，需人工拆分季集"
                    continue
                season = int(record.get("season") or 0)
                episode = int(record.get("episode") or 0)
                if tmdb_id not in seasons_cache:
                    seasons_cache[tmdb_id] = self._seasons_of(ctx, tmdb_id, state) or []
                listed = seasons_cache[tmdb_id]
                if not season:
                    real = [item for item in listed if item > 0]
                    if len(real) == 1:
                        # 只有一季：TMDB 编号就是第 1 季，可以确定
                        season = real[0]
                        record["season"] = season
                    elif not listed:
                        # 拿不到季列表：按第 1 季处理并标记待复核
                        season = 1
                        record["season"] = 1
                        record["status"] = "needs_review"
                        record["reason"] = "文件名无季号且 TMDB 季列表不可用，暂按第 1 季处理，请核对"
                        if not episode:
                            continue
                    else:
                        record["status"] = "needs_review"
                        record["reason"] = (
                            "无法确定季号，需人工指定。TMDB 已列出："
                            + ", ".join(f"S{item:02d}" for item in listed)
                        )
                        continue
                if not episode:
                    record["status"] = "needs_review"
                    record["reason"] = f"匹配到《{record['title']}》（S{season:02d}）但无法识别集数"
                    continue
                # 季号是否真实存在：避免「S02 但该剧只有一季」这类文件名错误。
                # 只在拿到非空季列表时判定，空列表/查询失败一律跳过，避免把
                # 「拿不到数据」误判成「季不存在」。
                if tmdb_id not in seasons_cache:
                    seasons_cache[tmdb_id] = self._seasons_of(ctx, tmdb_id, state) or []
                listed = seasons_cache[tmdb_id]
                if listed and season not in listed:
                    record["status"] = "needs_review"
                    record["reason"] = (
                        f"TMDB 未列出第 {season} 季，已列出："
                        + ", ".join(f"S{item:02d}" for item in listed)
                    )
                    continue
            else:
                record["season"] = 0
                record["episode"] = 0

            record["tmdbId"] = tmdb_id
            record["title"] = detail.get("title") or record["title"]
            record["originalTitle"] = detail.get("originalTitle") or record.get("originalTitle", "")
            record["year"] = year or record["year"]
            record["status"] = "matched"
            record["reason"] = ""
        return results, state

    @staticmethod
    def _resolve_unique(ctx: Context, record: dict,
                        details: dict[tuple[int, str], dict | None],
                        state: dict, tolerance: int) -> tuple[int, str]:
        """模型未给 ID 时，按名称+年份搜索；只有唯一候选通过校验才采纳。

        返回 (tmdbId, 说明)。多个候选或没有候选时返回 (0, 原因)。
        """
        media_type = record.get("mediaType")
        title = record.get("title") or ""
        original = record.get("originalTitle") or ""
        if media_type not in ("movie", "tv") or not (title or original):
            return 0, ""
        if state.get("tmdbSearches", 0) >= TMDB_SEARCH_LIMIT:
            return 0, ""

        candidates: list[dict] = []
        seen: set[int] = set()
        for query in (title, original):
            if not query:
                continue
            state["tmdbSearches"] = state.get("tmdbSearches", 0) + 1
            try:
                found = ctx.mt.tmdb.search(query=query, media_type=media_type)
            except SDKError as error:
                if error.code == "CANCELLED":
                    raise
                continue
            for item in found if isinstance(found, list) else []:
                if not isinstance(item, dict):
                    continue
                item_id = int(item.get("id") or 0)
                if item_id <= 0 or item_id in seen:
                    continue
                if item.get("mediaType") and item["mediaType"] != media_type:
                    continue
                seen.add(item_id)
                candidates.append(item)

        if not candidates:
            return 0, "模型未给出 TMDB ID，按名称搜索也没有结果"

        # 只有同时满足「名称命中」和「年份容差」的候选才算数
        wanted = [title, original]
        year = int(record.get("year") or 0)
        valid: list[dict] = []
        for item in candidates:
            names = [item.get("title", ""), item.get("originalTitle", "")]
            if not identify.title_matches(names, wanted):
                continue
            item_year = Plugin._year(item.get("year", ""))
            if year and item_year and abs(year - item_year) > tolerance:
                continue
            valid.append(item)

        if len(valid) == 1:
            return int(valid[0]["id"]), ""
        if len(valid) > 1:
            listed = "、".join(
                f"{item.get('title') or item.get('originalTitle')}（{item.get('year') or '?'}，ID {item.get('id')}）"
                for item in valid[:4]
            )
            return 0, f"同名候选无法裁决，需人工确认：{listed}"
        return 0, "模型未给出 TMDB ID，按名称搜索到的候选都与名称或年份不符"

    @staticmethod
    def _resolve_media_type(ctx: Context, tmdb_id: int,
                            details: dict[tuple[int, str], dict | None], state: dict) -> str:
        """模型给了 ID 但类型未知时，用 TMDB 反查它到底是电影还是剧集。"""
        for candidate in ("movie", "tv"):
            key = (tmdb_id, candidate)
            if key not in details:
                if state["tmdbDetails"] >= TMDB_SEARCH_LIMIT:
                    return ""
                try:
                    details[key] = ctx.mt.tmdb.detail(id=tmdb_id, media_type=candidate)
                except SDKError as error:
                    if error.code == "CANCELLED":
                        raise
                    details[key] = None
                state["tmdbDetails"] += 1
            detail = details[key]
            if detail and int(detail.get("id") or 0) == tmdb_id:
                return candidate
        return ""

    @staticmethod
    def _seasons_of(ctx: Context, tmdb_id: int, state: dict) -> list[int] | None:
        """拉取该剧在 TMDB 已列出的季号。

        返回 None 表示查询不可用（失败或额度用尽）——此时跳过季号校验，不误拦。
        返回空列表表示 TMDB 确实没有列出任何季。
        """
        if state.get("seasonQueries", 0) >= 60:
            return None
        state["seasonQueries"] = state.get("seasonQueries", 0) + 1
        try:
            listed = ctx.mt.tmdb.season(id=tmdb_id, season=0)
        except SDKError as error:
            if error.code == "CANCELLED":
                raise
            ctx.mt.log(level="debug", message=f"季列表查询失败 {error.code}，跳过季号校验")
            return None
        if not isinstance(listed, list):
            return None
        return sorted({int(item) for item in listed if isinstance(item, int) and item >= 0})

    @staticmethod
    def _year(value) -> int:
        text = str(value or "").strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits[:4]) if len(digits) >= 4 else 0

    # ---------------------------------------------------------------- 同剧一致性

    @staticmethod
    def _check_series(results: list[dict]) -> list[dict]:
        """同授权根、同目录、同规范化剧名的多集必须指向同一个 TMDB 作品与季号。"""
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for record in results:
            if record["status"] != "matched" or record["mediaType"] != "tv":
                continue
            path = record.get("path", "")
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            key = (record.get("rootId", ""), folder, naming.normalize(record.get("title", "")))
            groups.setdefault(key, []).append(record)
        for members in groups.values():
            if len(members) < 2:
                continue
            ids = {member.get("tmdbId") for member in members}
            seasons = {member.get("season") for member in members}
            if len(ids) > 1:
                for member in members:
                    member["status"] = "identity_conflict"
                    member["reason"] = "同剧跨批识别出不同 TMDB 作品，整组暂停"
            elif len(seasons) > 1:
                for member in members:
                    member["status"] = "identity_conflict"
                    member["reason"] = "同剧识别出不同季号，整组暂停"
        return results

    # ---------------------------------------------------------------- 提交

    def _submit_drafts(self, ctx: Context, flow_key: str, ready: list[dict], profile_id: int) -> int:
        created = 0
        total = len(ready)
        for start in range(0, len(ready), 100):
            chunk = ready[start:start + 100]
            self._report(ctx, "生成草稿", start, total,
                         f"正在处理 {chunk[0].get('name', '') if chunk else ''}")
            items = []
            for record in chunk:
                item = {
                    "fileRef": record["ref"],
                    "profileId": profile_id,
                    "tmdbId": int(record["tmdbId"]),
                    "mediaType": record["mediaType"],
                    "reason": "AI 识别并核对 TMDB",
                    "recognition": {
                        "title": record["title"] or record["name"],
                        "sources": "ai" if record.get("aiUsed") else "filename",
                    },
                }
                if record.get("originalTitle"):
                    item["recognition"]["originalTitle"] = record["originalTitle"]
                if record.get("year"):
                    item["recognition"]["year"] = int(record["year"])
                if record["mediaType"] == "tv":
                    item["season"] = int(record["season"])
                    item["episode"] = int(record["episode"])
                items.append(item)
            try:
                response = ctx.mt.organize.manual.prepare_batch(items=items)
            except SDKError as error:
                ctx.mt.log(level="error", message=f"草稿准备失败 {error.code}")
                for record in chunk:
                    record["status"] = "failed"
                    record["reason"] = f"草稿准备失败：{error.code}"
                continue
            prepared = {entry.get("fileRef"): entry for entry in response.get("items", [])
                        if isinstance(entry, dict)}
            for record in chunk:
                entry = prepared.get(record["ref"])
                if entry and entry.get("id"):
                    record["status"] = "submitted"
                    record["draftId"] = entry["id"]
                    record["reason"] = "已生成草稿，等待人工确认"
                    created += 1
                else:
                    record["status"] = "failed"
                    record["reason"] = "草稿未生成，请检查文件版本与方案"
            self._report(ctx, "草稿已生成", min(start + len(chunk), total), total,
                         chunk[-1].get("name", "") if chunk else "")
        self._persist(ctx, flow_key, ready)
        return created

    def _submit_direct(self, ctx: Context, flow_key: str, ready: list[dict], profile_id: int) -> list[str]:
        refs: list[str] = []
        total = len(ready)
        for index, record in enumerate(ready):
            ctx.mt.runtime.checkpoint()
            self._report(ctx, "提交整理", index, total, record.get("name", ""))
            key = f"{ctx.flow_id}:{record['ref']}:{record['tmdbId']}"
            payload = {
                "fileRef": record["ref"],
                "profileId": profile_id,
                "tmdbId": int(record["tmdbId"]),
                "mediaType": record["mediaType"],
                "idempotencyKey": key,
            }
            if record["mediaType"] == "tv":
                payload["season"] = int(record["season"])
                payload["episode"] = int(record["episode"])
            try:
                task = ctx.mt.organize.manual.run(payload)
            except SDKError as error:
                record["status"] = "failed"
                record["reason"] = f"提交失败：{error.code}"
                continue
            record["status"] = "submitted"
            record["taskRef"] = task["ref"]
            record["taskStatus"] = task.get("status", "queued")
            record["reason"] = "已入队"
            refs.append(task["ref"])
        self._patch_flow(ctx, flow_key, {"taskRefs": refs, "organizeParent": ctx.run_id,
                                         "stage": "organize"})
        self._persist(ctx, flow_key, ready)
        return refs

    @continuation("collect")
    def collect(self, input: dict, ctx: Context) -> dict:
        flow_key = ctx.flow_id
        flow = self._load_flow(ctx, flow_key)
        tasks = (ctx.event or {}).get("tasks") or []
        expected = set(flow.get("taskRefs") or [])
        if expected and {task.get("ref") for task in tasks} != expected:
            raise SDKError("续接事件与本次整理任务不匹配", "FLOW_MISMATCH")

        results = self._load(ctx, flow_key)
        by_ref = {record.get("taskRef"): record for record in results if record.get("taskRef")}
        settled = 0
        failed = 0
        for task in tasks:
            record = by_ref.get(task.get("ref"))
            if record is None:
                continue
            status = task.get("status", "")
            record["taskStatus"] = status
            if status == "done":
                record["status"] = "organized"
                record["reason"] = "整理完成"
                settled += 1
            elif status in ("failed", "cancelled", "interrupted", "outcome_unknown"):
                record["status"] = "failed"
                record["reason"] = f"整理任务 {status}"
                failed += 1
            else:
                record["reason"] = f"任务状态 {status}"
        self._persist(ctx, flow_key, results)
        counts = self._counts(results)
        message = f"整理结束：成功 {settled}，失败 {failed}"
        self._patch_summary(ctx, flow_key, {"status": "done", "counts": counts, "message": message})
        ctx.mt.runtime.result(value={"counts": counts, "message": message})
        ctx.mt.log(level="info", message=message)
        requested_mode = flow.get("mode")
        mode = "draft" if requested_mode == "draft" else "direct"
        stored = self._load_flow(ctx, flow_key).get("profileName")
        profile_name = stored if isinstance(stored, str) else ""
        self._notify_result(ctx, counts, mode, profile_name, results)
        return {"counts": counts, "settled": settled, "failed": failed}

    # ---------------------------------------------------------------- 查询

    @query("results")
    def results(self, input: dict, ctx: Context) -> dict:
        current = ctx.mt.storage.get(key="currentRun")
        if not current["found"]:
            return {"items": [], "nextCursor": "", "message": "尚未开始识别",
                    "status": "", "filter": "all", "filtered": 0}
        flow_key = current["value"]
        summary_entry = ctx.mt.storage.get(key=f"runs/{flow_key}/summary")
        summary = summary_entry["value"] if summary_entry["found"] else {}
        status_filter = str(input.get("status") or "all")
        if status_filter not in RESULTS_FILTERS:
            status_filter = "all"

        prefix = f"runs/{flow_key}/items/"
        # 选「全部」才用游标分页；按状态筛选时在服务端过滤，
        # 每页 20 条按命中结果算，游标记住「已处理到第几个原始条目」。
        offset = 0
        if status_filter != "all":
            cursor = str(input.get("cursor") or "")
            offset = int(cursor) if cursor.isdigit() else 0
        else:
            cursor = input.get("cursor", "")
            if cursor and not cursor.startswith(prefix):
                cursor = ""

        items: list[dict] = []
        scanned = 0
        next_cursor = ""
        while True:
            page = ctx.mt.storage.list(
                prefix=prefix,
                cursor="" if status_filter != "all" else cursor,
                limit=200 if status_filter != "all" else 20,
                values=True)
            batch = page.get("items", [])
            following = page.get("nextCursor") or ""
            for entry in batch:
                record = entry.get("value") or {}
                if status_filter != "all":
                    scanned += 1
                    if scanned <= offset:
                        continue
                    if record.get("status") != status_filter:
                        continue
                items.append({
                    "id": entry["key"],
                    "name": record.get("name", ""),
                    "title": record.get("title", ""),
                    "tmdb": str(record.get("tmdbId") or ""),
                    "episodeLabel": self._episode_label(record),
                    "statusLabel": self._status_label(record.get("status", "")),
                    "reason": record.get("reason", ""),
                    "aiRaw": (record.get("preview") or {}).get("text", "")[:600],
                })
                if status_filter != "all" and len(items) >= 20:
                    break
            if status_filter == "all":
                next_cursor = following
                break
            filled = len(items) >= 20
            if filled or not following:
                # 游标 = 服务端已消费的原始条目数；只要还有没读完的条目就允许继续翻
                exhausted = not following and not filled
                next_cursor = "" if exhausted else str(scanned)
                break
            cursor = following

        counts = summary.get("counts") or {}
        message = summary.get("message") or "尚未开始识别"
        if counts:
            message = f"{message}（共 {counts.get('total', 0)}，可整理 {counts.get('matched', 0)}，" \
                      f"已提交 {counts.get('submitted', 0)}，失败 {counts.get('failed', 0)}）"
        if status_filter != "all":
            label = self._status_label(status_filter)
            message = f"{message} · 正在只看「{label}」"
        return {"items": items, "nextCursor": next_cursor, "message": message,
                "status": summary.get("status", ""), "filter": status_filter,
                "filtered": len(items)}

    @query("tasks")
    def tasks(self, input: dict, ctx: Context) -> dict:
        """待处理的整理任务列表：只列进行中与等待中的，已结束（含已取消）的不再占位。"""
        try:
            offset = max(0, int(str(input.get("cursor") or "0")))
        except (TypeError, ValueError):
            offset = 0
        rows = self._task_rows(ctx, self._current_flow(ctx))
        if not rows:
            return {"items": [], "nextCursor": "", "total": 0, "cancellable": 0,
                    "message": "本轮没有整理任务", "current": ""}
        pending = [row for row in rows if row["active"]]
        done = sum(1 for row in rows if row["status"] == "done")
        cancelled = sum(1 for row in rows if row["status"] == CANCELLED_STATE)
        failed = sum(1 for row in rows if row["status"] in FAILED_STATES)
        running = [row for row in rows if row["status"] == "running"]
        current = f"正在整理 · {running[0]['label']}" if running else ""
        if not pending:
            return {"items": [], "nextCursor": "", "total": 0, "cancellable": 0,
                    "done": done, "cancelled": cancelled, "failed": failed,
                    "current": "",
                    "message": f"没有待处理的整理任务（已完成 {done}，已取消 {cancelled}，失败 {failed}）"}
        page = pending[offset:offset + TASK_PAGE_SIZE]
        following = offset + TASK_PAGE_SIZE
        return {
            "items": [{"ref": row["ref"], "label": row["label"],
                       "statusLabel": row["statusLabel"]} for row in page],
            "nextCursor": str(following) if following < len(pending) else "",
            "total": len(pending),
            "cancellable": len(pending),
            "done": done,
            "cancelled": cancelled,
            "failed": failed,
            "current": current,
            "message": f"当前 {len(pending)} 个待处理（已完成 {done}，已取消 {cancelled}，失败 {failed}）",
        }

    @action("cancel-task")
    def cancel_task(self, input: dict, ctx: Context) -> dict:
        ref = str(input.get("ref") or "").strip()
        if not ref:
            raise SDKError("缺少任务 ref", "NO_TASK_REF")
        flow_key = self._current_flow(ctx)
        known = {pair["ref"] for pair in self._load_taskmap(ctx, flow_key)} if flow_key else set()
        if ref not in known:
            raise SDKError("该任务不属于当前这一轮，不能从这里取消", "TASK_NOT_IN_FLOW")
        try:
            ok = ctx.mt.tasks.cancel(ref=ref)
        except SDKError as error:
            ctx.mt.log(level="error", message=f"取消任务失败：{error.code} {error}")
            raise
        ctx.mt.log(level="info", message=f"{'已取消' if ok else '未能取消'}整理任务 {ref}")
        self._maybe_close_flow(ctx)
        return {"ref": ref, "cancelled": bool(ok)}

    @action("cancel-all")
    def cancel_all(self, input: dict, ctx: Context) -> dict:
        rows = self._task_rows(ctx, self._current_flow(ctx))
        targets = [row for row in rows if row["cancellable"]][:CANCEL_LIMIT]
        cancelled = failed = 0
        for row in targets:
            try:
                if ctx.mt.tasks.cancel(ref=row["ref"]):
                    cancelled += 1
                else:
                    failed += 1
            except SDKError as error:
                failed += 1
                ctx.mt.log(level="warn", message=f"取消 {row['label'] or row['ref']} 失败：{error.code}")
        ctx.mt.log(level="info",
                   message=f"取消全部：尝试 {len(targets)}，成功 {cancelled}，失败 {failed}")
        self._maybe_close_flow(ctx)
        return {"total": len(rows), "attempted": len(targets),
                "cancelled": cancelled, "failed": failed}

    def _maybe_close_flow(self, ctx: Context) -> None:
        """取消后若已无待处理任务，就把流程标记结束。

        否则 summary.status 会一直停在 waiting，页面判定「运行中」，
        开始按钮被永久禁用 —— 用户就再也点不动了。
        """
        flow_key = self._current_flow(ctx)
        if not flow_key:
            return
        rows = self._task_rows(ctx, flow_key)
        if rows and any(row["active"] for row in rows):
            return
        self._patch_summary(ctx, flow_key,
                            {"status": "cancelled", "message": "整理任务已全部取消"})
        ctx.mt.log(level="info", message="整理任务已清空，本轮标记为结束")

    def _task_rows(self, ctx: Context, flow_key: str) -> list[dict]:
        if not flow_key:
            return []
        pairs = self._load_taskmap(ctx, flow_key)
        if not pairs:
            return []
        statuses = self._task_statuses(ctx, {pair["ref"] for pair in pairs})
        terminal = {"done", CANCELLED_STATE, *FAILED_STATES}
        rows = []
        for pair in pairs:
            status = statuses.get(pair["ref"], "")
            rows.append({
                "ref": pair["ref"],
                "label": pair["label"],
                "status": status,
                "statusLabel": self._task_status_label(status),
                "cancellable": status in CANCELABLE,
                "active": status not in terminal,
            })
        return rows

    @staticmethod
    def _current_flow(ctx: Context) -> str:
        entry = ctx.mt.storage.get(key="currentRun")
        return entry["value"] if entry["found"] else ""

    # 阶段名 → 步骤序号（0 扫描 / 1 识别 / 2 核对 / 3 提交 / 4 整理 / 5 归档）
    STEP_CURSOR = {
        "准备识别": 1, "识别中": 1, "已识别": 1,
        "核对 TMDB": 2,
        "生成草稿": 3, "草稿已生成": 3, "提交整理": 3, "已提交整理": 3,
    }

    @staticmethod
    def _step(label: str, owner: str, state: str, detail: str,
              completed: int = 0, total: int = 0) -> dict:
        return {"label": label, "owner": owner, "state": state, "detail": detail,
                "completed": int(completed), "total": int(total)}

    @staticmethod
    def _step_state(index: int, cursor: int) -> str:
        if index < cursor:
            return "已完成"
        if index == cursor:
            return "进行中"
        return "待开始"

    def _task_aggregate(self, ctx: Context, refs: list[str]) -> dict:
        """把一组任务 ref 汇总成计数 —— 用于 MT 执行的步骤。

        completed/total 用任务自己的文件数（扫描任务 total=400 那种），
        done/failed 是任务条数，两者对整理任务来说恰好相等。
        """
        wanted = {ref for ref in refs if ref}
        info = {"done": 0, "failed": 0, "running": 0, "pending": 0,
                "completed": 0, "total": 0, "active": False}
        if not wanted:
            return info
        cursor, found = "", {}
        for _ in range(20):
            page = ctx.mt.tasks.list(cursor=cursor, limit=100)
            for item in page.get("items", []):
                ref = item.get("ref")
                if ref in wanted and ref not in found:
                    found[ref] = item
            cursor = page.get("nextCursor") or ""
            if not cursor or len(found) >= len(wanted):
                break
        for ref in wanted:
            item = found.get(ref) or {}
            state = item.get("status") or ""
            total = int(item.get("total") or 1)
            done = int(item.get("completed") or 0)
            info["total"] += total
            if state == "done":
                info["done"] += 1
                info["completed"] += done if done else total
            elif state in ("failed", "cancelled", "interrupted", "outcome_unknown"):
                info["failed"] += 1
                info["completed"] += done
            elif state == "running":
                info["running"] += 1
                info["active"] = True
                info["completed"] += done
            else:
                info["pending"] += 1
                info["active"] = True
                info["completed"] += done
        return info

    def _build_steps(self, ctx: Context, flow: dict, summary: dict, status: str,
                     stage: str, done_count: int, total_in_step: int,
                     total_files: int) -> list[dict]:
        counts = summary.get("counts") or {}
        matched = int(counts.get("matched") or 0)
        submitted = int(counts.get("submitted") or 0)
        rows: list[dict] = []

        # 0 目录扫描：选了文件就跳过；选了目录要读 MT 的扫描任务
        scan_refs = [ref for ref in (flow.get("scanRefs") or []) if ref]
        if not scan_refs:
            rows.append(self._step("目录扫描", "MT", "跳过", "直接选了文件"))
        else:
            scan = self._task_aggregate(ctx, scan_refs)
            if status == "scanning":
                state = "进行中"
                detail = f"已扫 {scan['completed']}/{scan['total'] or '?'} 个"
            elif scan["failed"]:
                state = "已完成"
                detail = f"扫描 {scan['completed']} 个后停止（用已扫到的结果）"
            else:
                state = "已完成"
                detail = f"找到 {max(scan['completed'], total_files)} 个视频"
            rows.append(self._step("目录扫描", "MT", state, detail,
                                   scan["completed"], scan["total"]))

        # 1..3 插件自己的三段，用落盘的 stage 当游标
        cursor = self.STEP_CURSOR.get(stage, -1)
        if status in ("waiting", "submitting"):
            cursor = 4
        elif status == "done":
            cursor = 6
        elif status == "cancelled":
            cursor = 4

        if cursor > 1:
            rows.append(self._step("AI 识别", "AI", "已完成",
                                   f"{total_files}/{total_files} 个文件", total_files, total_files))
        elif cursor == 1:
            rows.append(self._step("AI 识别", "AI", "进行中",
                                   f"{done_count}/{total_in_step or total_files} 个文件",
                                   done_count, total_in_step or total_files))
        else:
            rows.append(self._step("AI 识别", "AI", "待开始", f"0/{total_files} 个文件",
                                   0, total_files))

        if cursor > 2:
            rows.append(self._step("TMDB 核对", "插件", "已完成",
                                   f"可整理 {matched}/{total_files}", matched, total_files))
        elif cursor == 2:
            rows.append(self._step("TMDB 核对", "插件", "进行中",
                                   f"{done_count}/{total_in_step or total_files} 个文件",
                                   done_count, total_in_step or total_files))
        else:
            rows.append(self._step("TMDB 核对", "插件", "待开始", f"0/{total_files}",
                                   0, total_files))

        if cursor > 3:
            rows.append(self._step("提交整理", "插件", "已完成",
                                   f"已提交 {submitted}/{matched}", submitted, matched))
        elif cursor == 3:
            rows.append(self._step("提交整理", "插件", "进行中",
                                   f"{done_count}/{total_in_step or matched} 个",
                                   done_count, total_in_step or matched))
        else:
            rows.append(self._step("提交整理", "插件", "待开始", f"0/{matched}", 0, matched))

        # 4 整理执行：MT 跑任务，进度靠 tasks.list
        task_refs = [ref for ref in (flow.get("taskRefs") or []) if ref]
        if not task_refs:
            rows.append(self._step("MT 整理", "MT", "待开始", "还没有提交任务"))
        else:
            org = self._task_aggregate(ctx, task_refs)
            settled = org["done"] + org["failed"]
            if status == "cancelled":
                state = "已取消"
                detail = f"完成 {org['done']}，其余已取消"
            elif settled >= len(task_refs):
                state = "已完成"
                detail = f"完成 {org['done']}，失败 {org['failed']}"
            else:
                state = "进行中"
                detail = (f"{settled}/{len(task_refs)}，进行中 {org['running']}，"
                          f"排队 {org['pending']}")
            rows.append(self._step("MT 整理", "MT", state, detail, settled, len(task_refs)))

        # 5 归档：collect 续接把真实结果回写
        if status == "done":
            rows.append(self._step("归档结果", "插件", "已完成",
                                   f"{submitted} 个已回写", submitted, submitted))
        elif status == "cancelled":
            rows.append(self._step("归档结果", "插件", "已取消", "本轮被取消"))
        else:
            rows.append(self._step("归档结果", "插件", "待开始", "等整理任务结束",
                                   0, submitted or 0))
        return rows

    @query("progress")
    def progress(self, input: dict, ctx: Context) -> dict:
        """刷新页面后 run.* 可能为空，这里用 storage 兜住最近一次进度。

        返回六步总览（steps），其中扫描与 MT 整理的进度是现查 tasks.list 得到的，
        所以「MT 在做什么」也看得见。
        """
        empty_steps: list[dict] = []
        flow_key = self._current_flow(ctx)
        if not flow_key:
            return {"completed": 0, "total": 0, "message": "", "stage": "",
                    "status": "", "active": False, "steps": empty_steps,
                    "stepLabels": [], "stepIndex": 0}
        flow = self._load_flow(ctx, flow_key) or {}
        entry = ctx.mt.storage.get(key=f"runs/{flow_key}/progress")
        data = entry["value"] if entry["found"] else {}
        summary_entry = ctx.mt.storage.get(key=f"runs/{flow_key}/summary")
        summary = summary_entry["value"] if summary_entry["found"] else {}
        status = str(summary.get("status") or "")
        counts = summary.get("counts") or {}
        total_files = int(counts.get("total") or data.get("total") or 0)
        stage = str(data.get("stage") or "")
        done_count = int(data.get("completed") or 0)
        total_in_step = int(data.get("total") or 0)
        identify_total = total_files or total_in_step

        stale = status in ("running", "identifying", "scanning") \
            and _age_minutes(str(data.get("at") or "")) > STALE_MINUTES
        steps = self._build_steps(ctx, flow, summary, status, stage,
                                  done_count, total_in_step, total_files)
        labels = [row["label"] for row in steps]
        cursor = next((i for i, row in enumerate(steps) if row["state"] == "进行中"), None)
        if cursor is None:
            # 没有进行中的步骤时，用「已完成（含跳过）」的个数当游标，进度条才不会倒退
            cursor = sum(1 for row in steps if row["state"] in ("已完成", "跳过"))
        cursor = max(0, min(cursor, len(steps) - 1))

        def out(**kw: Any) -> dict:
            return {"steps": steps, "stepLabels": labels, "stepIndex": cursor, **kw}

        if stale:
            for row in steps:
                if row["state"] == "进行中":
                    row["state"] = "已中断"
                    break
            return out(completed=identify_total, total=identify_total,
                       message="上一轮已中断 · 可以直接重新开始",
                       stage="stale", status="stale", active=False, current="")

        current = next((row for row in steps if row["state"] == "进行中"), None)
        if status in ("waiting", "submitting"):
            rows = self._task_rows(ctx, flow_key)
            if rows and not any(row["active"] for row in rows):
                return out(completed=identify_total, total=identify_total,
                           message="整理已结束 · 结果见下方识别结果",
                           stage="done", status="done", active=False, current="")
            return out(completed=identify_total, total=identify_total,
                       message="识别完成 · 整理由 MT 执行，进度见下方「整理任务」",
                       stage="organize", status=status, active=True,
                       current=current["label"] if current else "")
        if status in ("done", "cancelled"):
            message = ("整理任务已全部取消 · 结果见下方识别结果" if status == "cancelled"
                       else "本轮已结束 · 结果见下方识别结果")
            return out(completed=identify_total, total=identify_total,
                       message=message, stage="done", status=status,
                       active=False, current="")

        if current is None:
            completed, total = identify_total, identify_total
            message = "本轮已结束 · 结果见下方识别结果"
        else:
            completed = int(current.get("completed") or 0)
            total = int(current.get("total") or 0)
            message = f"{current['label']} · {current['detail']}"
        return out(completed=completed, total=total, message=message,
                   stage=stage, status=status, active=current is not None,
                   current=current["label"] if current else "")

    @staticmethod
    def _task_status_label(status: str) -> str:
        return {
            "running": "正在整理",
            "queued": "排队中",
            "submitting": "提交中",
            "waiting": "等待中",
            "done": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "interrupted": "被中断",
            "outcome_unknown": "结果未知",
        }.get(status, "状态未知")

    @classmethod
    def _task_label(cls, record: dict) -> str:
        title = record.get("title") or record.get("name") or ""
        episode = cls._episode_label(record)
        if episode and episode != "-":
            title = f"{title} {episode}".strip()
        return title[:60]

    @classmethod
    def _load_taskmap(cls, ctx: Context, flow_key: str) -> list[dict]:
        """从已落盘的识别结果里取出 任务 ref → 片名 映射。

        识别结果每轮都会 _persist，提交整理后每条记录带 taskRef，
        所以不必另存一份映射 —— 也让升级前提交的旧运行照样能查到进度。
        """
        prefix = f"runs/{flow_key}/items/"
        pairs: list[dict] = []
        cursor = ""
        for _ in range(10):  # 单轮上限 400，100/页 → 最多 4 页
            page = ctx.mt.storage.list(prefix=prefix, cursor=cursor, limit=100, values=True)
            for entry in page.get("items", []):
                record = entry.get("value") or {}
                ref = record.get("taskRef")
                if ref:
                    pairs.append({"ref": ref, "label": cls._task_label(record)})
            cursor = page.get("nextCursor") or ""
            if not cursor:
                break
        return pairs

    @staticmethod
    def _task_statuses(ctx: Context, refs: set[str]) -> dict[str, str]:
        """分页扫 tasks.list，只留下本次提交的任务状态。"""
        found: dict[str, str] = {}
        cursor = ""
        for _ in range(20):
            page = ctx.mt.tasks.list(cursor=cursor, limit=100)
            for item in page.get("items", []):
                ref = item.get("ref")
                if ref in refs:
                    found[ref] = item.get("status") or ""
            cursor = page.get("nextCursor") or ""
            if not cursor or len(found) >= len(refs):
                break
        return found

    @staticmethod
    def _episode_label(record: dict) -> str:
        if record.get("mediaType") != "tv":
            return "-"
        season = record.get("season") or 0
        episode = record.get("episode") or 0
        return f"S{season:02d}E{episode:02d}" if season or episode else "-"

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "identified": "已识别",
            "matched": "已核对",
            "unrecognized": "未识别",
            "identity_conflict": "身份冲突",
            "needs_review": "需人工处理",
            "submitted": "已提交",
            "organized": "已完成",
            "failed": "失败",
        }.get(status, status or "未知")

    # ---------------------------------------------------------------- 辅助

    @staticmethod
    def _selected(ctx: Context) -> tuple[list[dict], list[dict]]:
        files: list[dict] = []
        dirs: list[dict] = []
        seen: set[str] = set()
        for item in ctx.files or []:
            if item.get("isDir"):
                dirs.append(item)
                continue
            if item["ref"] in seen:
                continue
            seen.add(item["ref"])
            if naming.is_video(item.get("name", "")):
                files.append(item)
        return files, dirs

    @staticmethod
    def _profile_id(input: dict, ctx: Context) -> int:
        # profile 是动作表单字段（type=profile），config 里不再声明
        raw = input.get("profileId")
        try:
            profile_id = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            profile_id = 0
        if profile_id <= 0:
            raise SDKError("请先在设置中选择一个整理方案", "NO_PROFILE")
        return profile_id

    @staticmethod
    def _mode(input: dict, ctx: Context) -> str:
        value = input.get("mode") or ctx.config.get("mode") or "draft"
        return "direct" if value == "direct" else "draft"

    @staticmethod
    def _options(ctx: Context) -> dict:
        config = ctx.config
        batch = config.get("batchSize")
        batch = int(batch) if isinstance(batch, (int, float)) else DEFAULT_BATCH
        tolerance = config.get("yearTolerance")
        tolerance = int(tolerance) if isinstance(tolerance, (int, float)) else DEFAULT_TOLERANCE
        minutes = config.get("maxMinutes")
        minutes = int(minutes) if isinstance(minutes, (int, float)) else DEFAULT_MINUTES
        extra = config.get("promptExtra") or ""
        return {
            "batch": max(1, min(10, batch)),
            "tolerance": max(0, min(5, tolerance)),
            "minutes": max(2, min(60, minutes)),
            "nativeSearch": bool(config.get("nativeSearch")),
            "extra": extra if isinstance(extra, str) else "",
            "minConfidence": 0.5,
        }

    def _check_profile(self, ctx: Context, profile_id: int) -> dict:
        """校验方案可用性。

        重要：**不检查 destination.writable**。本插件不申请 files.write，
        授权页因此不提供写入勾选项，方案的目标目录必然是 writable=false。
        真正的写入权限由宿主在执行 organize.manual.run 时按方案自己校验——
        官方模板也是直接把 profileId 交给宿主，从不预检目标目录。
        """
        try:
            profiles = ctx.mt.organize.profiles()
        except SDKError as error:
            if error.code == "NO_DESTINATION":
                # 宿主自己拒绝：方案的目标目录没有写入授权。给可执行的提示，别让它变成「点了没反应」。
                message = ("所选整理方案的目标目录没有写入授权。请在 MT 的方案设置里"
                           "给目标目录补上写权限，或换一个目标可写的方案后重试")
                ctx.mt.log(level="error", message=f"{message}（{error.code}）")
                raise SDKError(message, "NO_DESTINATION") from error
            message = f"无法读取整理方案（{error.code}），请检查 organize.read 授权"
            ctx.mt.log(level="error", message=message)
            raise SDKError(message, "NO_PROFILE") from error

        found = None
        for profile in profiles or []:
            if int(profile.get("id", 0)) == profile_id:
                found = profile
                break
        if found is None:
            available = ", ".join(
                f"{item.get('id')} {item.get('name', '')}" for item in (profiles or [])[:10]
            ) or "无"
            message = f"找不到整理方案 {profile_id}。当前可用方案：{available}"
            ctx.mt.log(level="error", message=message)
            raise SDKError(message, "NO_PROFILE")

        name = found.get("name", "")
        if found.get("kind") != "organize":
            message = f"方案「{name}」不是整理方案（kind={found.get('kind')}）"
            ctx.mt.log(level="error", message=message)
            raise SDKError(message, "INVALID_PROFILE")
        if not found.get("enabled"):
            message = f"方案「{name}」已停用，请先在自动整理页面启用"
            ctx.mt.log(level="error", message=message)
            raise SDKError(message, "INVALID_PROFILE")

        source = found.get("source") or {}
        destination = found.get("destination") or {}
        ctx.mt.log(
            level="info",
            message=(f"使用方案「{name}」：源 {source.get('path', '-')} → "
                     f"目标 {destination.get('path', '-') if destination else '（宿主未返回）'}。"
                     "目标目录的写入权限由宿主在执行整理时校验。"),
        )
        return found

    def _notify_result(self, ctx: Context, counts: dict, mode: str,
                       profile_name: str, results: list[dict]) -> None:
        """把本轮结果发到通知通道。失败只记日志，绝不影响整理结果。"""
        if not self._notify_enabled(ctx):
            return
        try:
            title, body = notify.build_summary(counts, mode, profile_name, results)
            channel = self._notify_channel(ctx)
            if not channel:
                ctx.mt.log(level="warn", message="通知已开启但未选择通知通道，跳过发送")
                return
            ctx.mt.notification.send(channel_id=channel, title=title, content=body)
            ctx.mt.log(level="info", message=f"已发送结果通知：{title}")
        except SDKError as error:
            # 通知失败不能影响已经完成的整理
            ctx.mt.log(level="warn", message=f"发送通知失败 {error.code}：{error}")
        except Exception as error:  # noqa: BLE001
            ctx.mt.log(level="warn", message=f"发送通知异常：{type(error).__name__}")

    @staticmethod
    def _notify_enabled(ctx: Context) -> bool:
        """默认开启；只有显式配置为 false 才关闭。"""
        value = ctx.config.get("notifyEnabled")
        return True if value is None else bool(value)

    @staticmethod
    def _notify_channel(ctx: Context) -> str:
        """优先用配置里指定的通道；留空时用宿主授予的第一个通道。"""
        channel = ctx.config.get("notifyChannelId")
        if isinstance(channel, str) and channel.strip():
            return channel.strip()
        try:
            resources = ctx.mt.runtime.resources()
        except SDKError:
            return ""
        if not isinstance(resources, dict):
            return ""
        granted = resources.get("notificationChannelIds") or []
        for item in granted:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
        return ""

    @staticmethod
    def _capabilities(ctx: Context) -> dict:
        try:
            return ctx.mt.ai.capabilities() or {}
        except SDKError as error:
            ctx.mt.log(level="warn", message=f"AI 能力查询失败 {error.code}")
            return {"chat": {"available": False, "reason": "unavailable"}}

    @staticmethod
    def _record(item: dict, status: str = "identified", reason: str = "") -> dict:
        return {
            "ref": item["ref"],
            "name": item.get("name", ""),
            "path": item.get("path", ""),
            "rootId": item.get("rootId", ""),
            "version": item.get("version", ""),
            "status": status,
            "reason": reason,
            "title": "",
            "originalTitle": "",
            "year": 0,
            "mediaType": "movie",
            "season": 0,
            "episode": 0,
            "tmdbId": 0,
            "multiEpisode": False,
            "confidence": 0.0,
            "aiUsed": False,
            "updatedAt": _now(),
        }

    @staticmethod
    def _counts(results: list[dict]) -> dict:
        counts = {"total": len(results), "matched": 0, "submitted": 0, "failed": 0,
                  "unrecognized": 0, "identity_conflict": 0, "needs_review": 0}
        for record in results:
            status = record.get("status")
            if status == "matched":
                counts["matched"] += 1
            elif status in ("submitted", "organized"):
                # 已提交的文件此前一定通过了 TMDB 核对，仍计入「可整理」
                counts["matched"] += 1
                counts["submitted"] += 1
            elif status == "identity_conflict":
                counts["identity_conflict"] += 1
            elif status == "needs_review":
                counts["needs_review"] += 1
            elif status == "unrecognized":
                counts["unrecognized"] += 1
            elif status == "failed":
                counts["failed"] += 1
        return counts

    # ---------------------------------------------------------------- 进度

    def _report(self, ctx: Context, stage: str, done: int, total: int, detail: str = "") -> None:
        """把阶段、进度和当前文件名推给宿主进度条，并落盘一份。

        宿主进度条自己会显示 completed/total，所以 message 里**不要再写分数**，
        否则会出现「整理完成 0/7 · 0/7」这种重复。
        同时写一份到 storage：刷新页面后 run.* 可能是空的，页面靠它兜底。
        进度上报失败绝不能影响主流程。
        """
        clean = (detail or "").strip()
        if len(clean) > 56:
            clean = clean[:54] + "…"
        text = f"{stage} · {clean}" if clean else stage
        try:
            ctx.mt.runtime.progress(completed=int(done), total=int(total), message=text)
        except SDKError:
            pass
        self._remember_progress(ctx, stage, int(done), int(total), text)

    def _remember_progress(self, ctx: Context, stage: str, done: int,
                           total: int, text: str) -> None:
        """节流落盘进度：阶段变化、每 10 个文件或收尾时写一次，避免把 storage 调用打满。"""
        marks = getattr(self, "_progress_marks", None)
        if marks is None:
            marks = self._progress_marks = {}
        mark = marks.get(ctx.flow_id)
        if mark is not None and mark[0] == stage and done % 10 and done != total:
            return
        marks[ctx.flow_id] = (stage, done)
        try:
            ctx.mt.storage.set(key=f"runs/{ctx.flow_id}/progress",
                               value={"completed": done, "total": total,
                                      "message": text, "stage": stage, "at": _now()})
        except SDKError:
            pass

    @staticmethod
    def _write_summary(ctx: Context, flow_key: str, summary: dict) -> None:
        ctx.mt.storage.set(key=f"runs/{flow_key}/summary", value={**summary, "updatedAt": _now()})

    def _patch_summary(self, ctx: Context, flow_key: str, patch: dict) -> None:
        entry = ctx.mt.storage.get(key=f"runs/{flow_key}/summary")
        current = entry["value"] if entry["found"] else {}
        self._write_summary(ctx, flow_key, {**current, **patch})

    @staticmethod
    def _persist(ctx: Context, flow_key: str, results: list[dict]) -> None:
        """逐文件分键写入，避免单个 KV 值随文件数增长。"""
        operations = []
        for index, record in enumerate(results):
            operations.append({
                "operation": "set",
                "key": f"runs/{flow_key}/items/{index:06d}",
                "value": record,
            })
            if len(operations) >= 50:
                ctx.mt.storage.batch(operations=operations)
                operations = []
        if operations:
            ctx.mt.storage.batch(operations=operations)

    @staticmethod
    def _save_flow(ctx: Context, flow_key: str, flow: dict) -> None:
        ctx.mt.storage.set(key=f"flows/{flow_key}", value={**flow, "updatedAt": _now()})

    @staticmethod
    def _load_flow(ctx: Context, flow_key: str) -> dict:
        entry = ctx.mt.storage.get(key=f"flows/{flow_key}")
        return entry["value"] if entry["found"] and isinstance(entry["value"], dict) else {}

    @classmethod
    def _patch_flow(cls, ctx: Context, flow_key: str, patch: dict) -> dict:
        current = cls._load_flow(ctx, flow_key)
        merged = {**current, **patch}
        cls._save_flow(ctx, flow_key, merged)
        return merged

    @staticmethod
    def _save_pending(ctx: Context, flow_key: str, files: list[dict]) -> None:
        for start in range(0, len(files), 100):
            chunk = files[start:start + 100]
            ctx.mt.storage.set(key=f"pending/{flow_key}/chunk/{start // 100:03d}", value=chunk)

    @staticmethod
    def _load_pending(ctx: Context, flow_key: str) -> list[dict]:
        files: list[dict] = []
        cursor = ""
        prefix = f"pending/{flow_key}/chunk/"
        while True:
            page = ctx.mt.storage.list(prefix=prefix, cursor=cursor, limit=50, values=True)
            for entry in page.get("items", []):
                files.extend(entry.get("value") or [])
            following = page.get("nextCursor", "")
            if not following or following == cursor:
                break
            cursor = following
        return files

    @staticmethod
    def _load(ctx: Context, flow_key: str) -> list[dict]:
        results: list[dict] = []
        cursor = ""
        prefix = f"runs/{flow_key}/items/"
        while True:
            page = ctx.mt.storage.list(prefix=prefix, cursor=cursor, limit=100, values=True)
            for entry in page.get("items", []):
                value = entry.get("value")
                if isinstance(value, dict):
                    results.append(value)
            following = page.get("nextCursor", "")
            if not following or following == cursor:
                break
            cursor = following
        return results
