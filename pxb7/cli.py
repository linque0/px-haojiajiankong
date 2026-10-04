"""命令行入口（argparse 子命令）。

已实现：init-db / crawl / parse-raw / status / login
退出码：0 完成；21 风控终止（crawl）；1 错误；2 用法错误（argparse）。

路径语义：
- `--db` 等显式路径参数按调用时 CWD 解析（pxb7.config.resolve_cli_path）；
- 未传参时用 config/settings.yaml 的内部路径（以项目根为基准）。

约定：crawl 的 stdout 只打印摘要；完整指标写 `--summary-json`（键名照契约）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

from . import __version__
from .config import (
    ConfigError,
    all_warnings,
    load_config,
    load_settings,
    load_tasks,
    override_db_path,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_RISK_ABORT = 21
EXIT_QC_FAIL = 22
EXIT_NO_LOGIN = 24


# --------------------------------------------------------------------------- #
# init-db
# --------------------------------------------------------------------------- #
def _cmd_init_db(args: argparse.Namespace) -> int:
    from . import db, extract

    settings, tasks = load_config(args.settings, args.tasks)
    if args.db:
        settings = override_db_path(settings, args.db)

    for message in all_warnings(settings, tasks):
        print(f"[warn] {message}", file=sys.stderr)

    summary = db.init_db(
        settings.paths.db,
        tasks=[t.as_row() for t in tasks.tasks if t.enabled],
        refresh_tasks=args.refresh_tasks,
    )

    keyword_rows = extract.seed_db_rows()
    with db.connect(settings.paths.db) as conn:
        keyword_count = db.upsert_dim_keywords(conn, keyword_rows)

    print(f"数据库：{summary['db_path']}")
    print(f"schema 版本：{summary['schema_version']}；业务表 {summary['table_count']} 张")
    if summary["created_tables"]:
        print(f"本次新建表：{', '.join(summary['created_tables'])}")
    else:
        print("本次未新建表（已存在，幂等）")
    for table, count in summary["row_counts"].items():
        print(f"  {table:<24} {count}")
    print(f"词表种子：{keyword_count} 条 → dim_keyword（profile=genshin_10026）")
    if not args.refresh_tasks:
        print("dim_task 预置采用 INSERT OR IGNORE（不覆盖库内已有行的运行期改动）；"
              "需强制对齐 tasks.yaml 时加 --refresh-tasks")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
def _cmd_crawl(args: argparse.Namespace) -> int:
    from . import pipeline

    settings, tasks = load_config(args.settings, args.tasks)
    if args.db:
        settings = override_db_path(settings, args.db)
    task = tasks.by_id(args.task) if args.task else (
        tasks.enabled()[0] if tasks.enabled() else None)
    if task is None:
        print("[error] tasks.yaml 中没有可运行的 enabled 任务", file=sys.stderr)
        return EXIT_ERROR

    for message in all_warnings(settings, tasks):
        print(f"[warn] {message}", file=sys.stderr)

    mode = "guest" if args.guest else None
    result = pipeline.run_once(
        settings=settings, task=task, pages=args.pages, detail=args.detail or 0, mode=mode,
        run_id=args.run_id, summary_path=args.summary_json)

    summary = result.summary
    print(f"[crawl] run_id={summary['run_id']} task={summary['task_id']} "
          f"status={summary['status']} via={summary['collected_via']}")
    print(f"[crawl] pages={summary['pages']} cards_seen={summary['cards_seen']} "
          f"cards_parsed={summary['cards_parsed']} "
          f"parse_success_rate={summary['parse_success_rate']:.2%} "
          f"extract_hit_rate={summary['extract_hit_rate']:.2%}")
    if result.extraction_report:
        stats = result.extraction_report
        print(f"[crawl] 命中口径对照：extract_hit_rate={stats.get('extract_hit_rate', 0):.2%}"
              f"（契约：标题/卡片文本命中 ≥1 词表关键词）｜any_hit_rate="
              f"{stats.get('any_hit_rate', 0):.2%}（任一通道，含 [卡] 字段直取）"
              f"｜card_field_coverage={stats.get('card_field_coverage', 0):.2%}"
              f"（原石/纠缠等 [卡] 字段通道覆盖面，不计入契约口径）"
              f"｜命中来源 {stats.get('hits_by_via', {})}")
    print(f"[crawl] snapshots={summary['snapshots_inserted']} new_listings={summary['new_listings']} "
          f"price_changes={summary['price_changes']} delist_events={summary['delist_events']} "
          f"detail_fetched={summary['detail_fetched']}")
    print(f"[crawl] raw_dir={summary['raw_dir'] or '（本轮未落盘 raw）'} "
          f"duration={summary['duration_s']}s")
    if result.list_result and result.list_result.pages:
        if result.field_diagnostics:
            print("[crawl] 字段命中明细（校准用；* = 契约 required）：")
            for line in pipeline.format_field_table(result.field_diagnostics):
                print(line)
        for page in result.list_result.pages:
            wait = page.card_wait_selector or "未命中候选选择器"
            used = page.parse.card_selector_used if page.parse else None
            print(f"[crawl] p{page.page_no}: 等待命中={wait}；卡片选择器={used or '无'}；"
                  f"cards_seen={page.cards_seen} cards_parsed={page.cards_parsed}")
            if page.parse:
                for example in page.parse.failing_examples(2):
                    print(f"[crawl] p{page.page_no} 失败样例：{example}")
    else:
        print("[crawl] 本轮未获取到任何页面，无字段数据可诊断（不是解析失败，见下方风控原因）。")
    if summary["risk_trigger"]:
        detail = result.risk_detail or {}
        print(f"[crawl] 风控触发：{summary['risk_trigger']}（level={detail.get('level')}"
              f" backoff_until={detail.get('backoff_until')}"
              f" 剩余={detail.get('backoff_remaining_s')}s）", file=sys.stderr)
        if detail and not detail.get("requests_issued"):
            print("[crawl] 前置闸门拦截：本轮未发起任何请求（退避未到期，docs/01 §3.3 硬逻辑）。",
                  file=sys.stderr)
    if result.summary_path:
        print(f"[crawl] summary 已写入：{result.summary_path}")
    if result.report_path:
        print(f"[crawl] 抽取诊断：{result.report_path}")
    if result.notify:
        print(f"[crawl] 通知：{result.notify}")
    return EXIT_RISK_ABORT if result.aborted else EXIT_OK


# --------------------------------------------------------------------------- #
# parse-raw
# --------------------------------------------------------------------------- #
def _cmd_parse_raw(args: argparse.Namespace) -> int:
    from . import pipeline

    settings, tasks = load_config(args.settings, args.tasks)
    if args.db:
        settings = override_db_path(settings, args.db)
    if args.raw_root:
        import dataclasses
        settings = dataclasses.replace(
            settings, paths=dataclasses.replace(
                settings.paths, raw_root=Path(args.raw_root).expanduser().resolve()))
    task = tasks.by_id(args.task) if args.task else (
        tasks.enabled()[0] if tasks.enabled() else None)
    if task is None:
        print("[error] tasks.yaml 中没有可用于重放的任务", file=sys.stderr)
        return EXIT_ERROR

    if args.run_dir:
        run_dirs = [Path(args.run_dir).expanduser().resolve()]
    else:
        # --date 归一化：YYYY-MM-DD / YYYYMMDD 均可（终审发现项 #2/#32：此前按字面拼
        # 目录名，按 metavar 写 2026-10-02 会找不到目录）
        date_norm = re.sub(r"[^0-9]", "", args.date) if args.date else None
        run_dirs = pipeline.find_raw_runs(settings, task_id=task.task_id, date=date_norm)
    if not run_dirs:
        print(f"[parse-raw] 没有找到 raw run 目录（task={task.task_id} date={args.date}）",
              file=sys.stderr)
        return EXIT_ERROR

    failures = 0
    aborted = 0
    for run_dir in run_dirs:
        try:
            result = pipeline.replay_raw_dir(settings, run_dir=run_dir, task=task,
                                             infer_delist=args.infer_delist)
        except Exception as exc:
            failures += 1
            print(f"[parse-raw] {run_dir} 重放失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        s = result.summary
        print(f"[parse-raw] {run_dir.name}: cards_seen={s['cards_seen']} "
              f"cards_parsed={s['cards_parsed']} parse_success_rate={s['parse_success_rate']:.2%} "
              f"snapshots={s['snapshots_inserted']} price_changes={s['price_changes']} "
              f"new_listings={s['new_listings']} status={s['status']} "
              f"risk_trigger={s['risk_trigger']} → {result.summary_path}")
        if result.field_diagnostics:
            print("[parse-raw] 字段命中明细（校准用；* = 契约 required）：")
            for line in pipeline.format_field_table(result.field_diagnostics):
                print(line)
        if result.aborted:
            aborted += 1
            print(f"[parse-raw] 该 raw 目录是风控/拦截页面，已按「不入库」处理"
                  f"（reason={s['risk_trigger']}）；请勿当作正常列表页。", file=sys.stderr)
    if failures:
        return EXIT_ERROR
    if aborted:
        return EXIT_RISK_ABORT
    return EXIT_OK


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _cmd_status(args: argparse.Namespace) -> int:
    from . import db, risk

    settings = load_settings(args.settings)
    if args.db:
        settings = override_db_path(settings, args.db)

    payload: dict = {"db": str(settings.paths.db)}
    try:
        with db.connect(settings.paths.db, read_only=True) as conn:
            payload["tables"] = db.table_counts(conn)
            latest = db.latest_round(conn)
            payload["latest_round"] = latest.isoformat(timespec="seconds") if latest else None
            payload["tasks"] = [
                {"task_id": r[0], "game_id": r[1], "name": r[2], "pages_per_run": r[3],
                 "runs_per_day": r[4], "enabled": bool(r[5])}
                for r in conn.execute(
                    "SELECT task_id, game_id, task_name, pages_per_run, runs_per_day, enabled"
                    " FROM dim_task ORDER BY task_id").fetchall()
            ]
            payload["active_listings"] = conn.execute(
                "SELECT count(*) FROM dim_listing WHERE is_active").fetchone()[0]
            payload["keywords"] = conn.execute(
                "SELECT count(*), sum(CASE WHEN enabled THEN 1 ELSE 0 END) FROM dim_keyword"
            ).fetchone()[:2]
            payload["keyword_types"] = [
                {"keyword_type": r[0], "n": r[1]} for r in conn.execute(
                    "SELECT keyword_type, count(*) FROM dim_keyword GROUP BY 1 ORDER BY 1"
                ).fetchall()]
    except Exception as exc:
        payload["db_error"] = f"{type(exc).__name__}: {exc}"

    state = risk.RiskStateStore(settings.paths.risk_state).load()
    now = _dt.datetime.now()
    # 任务级档位（终审发现项 #17/#42：顶层镜像只反映「最近被操作的任务」，
    # 顶层的 level=normal 不代表所有任务都正常——必须逐任务展示）
    payload["risk_tasks"] = {
        task_id: {
            "level": entry.level, "reason": entry.reason,
            "backoff_until": entry.backoff_until.isoformat(timespec="seconds")
            if entry.backoff_until else None,
            "backoff_remaining_s": round(entry.backoff_remaining(now), 1),
            "consecutive": entry.consecutive, "probe_mode": entry.probe_mode,
            "frequency_factor": entry.frequency_factor, "raw_only": entry.raw_only,
        }
        for task_id, raw in state.tasks.items()
        for entry in [risk.TaskRisk.from_json(raw)]
    }
    if args.task:
        entry = risk.TaskRisk.from_json(state.tasks.get(args.task))
        payload["risk_state"] = {
            "task_id": args.task,
            "level": entry.level, "reason": entry.reason,
            "backoff_until": entry.backoff_until.isoformat(timespec="seconds")
            if entry.backoff_until else None,
            "backoff_remaining_s": round(entry.backoff_remaining(now), 1),
            "consecutive": entry.consecutive, "session_slot": state.session_slot,
            "probe_mode": entry.probe_mode, "frequency_factor": entry.frequency_factor,
            "raw_only": entry.raw_only,
            "observe_until": state.observe_until.isoformat(timespec="seconds")
            if state.observe_until else None,
        }
    else:
        payload["risk_state"] = {
            "level": state.level, "reason": state.reason,
            "backoff_until": state.backoff_until.isoformat(timespec="seconds")
            if state.backoff_until else None,
            "backoff_remaining_s": round(state.backoff_remaining(now), 1),
            "last_trigger": state.last_trigger.isoformat(timespec="seconds")
            if state.last_trigger else None,
            "consecutive": state.consecutive, "session_slot": state.session_slot,
            "probe_mode": state.probe_mode, "frequency_factor": state.frequency_factor,
            "raw_only": state.raw_only,
            "observe_until": state.observe_until.isoformat(timespec="seconds")
            if state.observe_until else None,
        }
    payload["rate_limit"] = risk.describe_rules(settings)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"数据库：{payload['db']}")
    if payload.get("db_error"):
        print(f"  [error] {payload['db_error']}")
    else:
        print(f"  最近轮次：{payload.get('latest_round')}；在售 listing：{payload.get('active_listings')}")
        for table, count in payload["tables"].items():
            print(f"  {table:<24} {count}")
        kw_total, kw_enabled = payload["keywords"]
        print(f"  词表：{kw_enabled}/{kw_total} 启用；类型分布 "
              f"{ {d['keyword_type']: d['n'] for d in payload['keyword_types']} }")
    rc = payload["risk_state"]
    label = f"（任务 {rc['task_id']}）" if rc.get("task_id") else ""
    print(f"风控状态{label}：level={rc['level']} reason={rc['reason']} "
          f"backoff_until={rc['backoff_until']}（剩余 {rc['backoff_remaining_s']}s）"
          f" consecutive={rc['consecutive']} slot={rc['session_slot']} "
          f"factor={rc['frequency_factor']} raw_only={rc['raw_only']}")
    for task_id, entry in sorted(payload.get("risk_tasks", {}).items()):
        print(f"  任务档位 {task_id:<20} level={entry['level']} reason={entry['reason']} "
              f"backoff_until={entry['backoff_until']}"
              f"（剩余 {entry['backoff_remaining_s']}s）factor={entry['frequency_factor']}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# login（人工扫码，单次尝试、不重试）
# --------------------------------------------------------------------------- #
def _cmd_login(args: argparse.Namespace) -> int:
    from . import browser

    settings = load_settings(args.settings)
    target = (settings.paths.storage_primary if args.slot == "primary"
              else settings.paths.storage_backup)
    target.parent.mkdir(parents=True, exist_ok=True)

    print(f"[login] 将打开有头浏览器，请在窗口中完成登录（扫码/账密），目标登录态：{target}")
    print("[login] 纪律：专用小号；只登录一次，不做连续重试；登录态文件不得入 git、不得外传。")
    if getattr(args, "auto_save", False):
        print("[login] auto-save 模式：检测到登录 Cookie（token+userId）后自动保存，"
              "无需回终端按回车；请在浏览器窗口里完成登录与滑块验证。")
    session = browser.BrowserSession(settings, mode="guest", headless=False)
    try:
        session.start()
    except browser.BrowserUnavailable as exc:
        print(f"[login] 无法启动浏览器：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        page = session.new_page()
        nav = session.goto(page, settings.site.base_url)
        if not nav.ok:
            print(f"[login] 打开站点失败：{nav.error}", file=sys.stderr)
            return EXIT_ERROR
        if getattr(args, "auto_save", False):
            # 轮询等待人工登录完成（含 WAF 滑块）：登录 Cookie 出现即自动保存。
            # 单次等待、不自动重试登录；超时退出不写任何文件。
            import time as _time
            deadline = _time.monotonic() + float(getattr(args, "timeout", 300))
            logged_in = False
            while _time.monotonic() < deadline:
                try:
                    names = {c.get("name") for c in session._context.cookies()}
                except Exception:
                    names = set()
                if {"token", "userId"} <= names:
                    logged_in = True
                    break
                _time.sleep(3)
            if not logged_in:
                print(f"[login] 等待超时（{args.timeout}s），未检测到登录 Cookie；未保存任何文件。",
                      file=sys.stderr)
                return EXIT_ERROR
            _time.sleep(5)                       # 留给站点写入会话/WAF Cookie
        else:
            try:
                input("[login] 完成登录后回到本终端按回车保存登录态（Ctrl+C 放弃）... ")
            except EOFError:
                print("[login] 非交互终端，无法等待人工登录；请在交互式终端运行本命令，"
                      "或使用 --auto-save。", file=sys.stderr)
                return EXIT_ERROR
        context = getattr(session, "_context", None)
        if context is None:
            print("[login] 会话上下文不可用，保存失败", file=sys.stderr)
            return EXIT_ERROR
        context.storage_state(path=str(target))
        from .browser import validate_storage_state
        ok, why = validate_storage_state(target)
        if not ok:
            print(f"[login] 已保存 {target}，但校验未通过：{why}", file=sys.stderr)
            print("[login] 说明登录可能未完成（未产生任何 cookie）；请重跑本命令重新登录。",
                  file=sys.stderr)
            return EXIT_ERROR
        print(f"[login] 已保存并校验通过：{target}（{why}）")
        print("[login] 下次 crawl 会优先使用它；若被风控请改用 --slot backup 或降级游客态。")
    except KeyboardInterrupt:
        print("[login] 已放弃（未保存）", file=sys.stderr)
        return EXIT_ERROR
    finally:
        session.stop()
    return EXIT_OK


# --------------------------------------------------------------------------- #
# extract / load / qc / session-gain
# --------------------------------------------------------------------------- #
def _cmd_extract(args: argparse.Namespace) -> int:
    """词表抽取器入库：config/keywords_seed.yaml → dim_keyword（幂等）。"""
    from . import db, extract

    settings, tasks = load_config(args.settings, args.tasks)
    if args.db:
        settings = override_db_path(settings, args.db)
    rows = extract.seed_db_rows()
    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row["keyword_type"]] = by_type.get(row["keyword_type"], 0) + 1
    print(f"[extract] 词表种子 {len(rows)} 条，类型分布 {by_type}"
          f"（启用 {sum(1 for r in rows if r['enabled'])} 条）")
    if args.dry_run:
        print("[extract] --dry-run：未写入数据库")
        return EXIT_OK
    with db.connect(settings.paths.db) as conn:
        written = db.upsert_dim_keywords(conn, rows)
        total, enabled = conn.execute(
            "SELECT count(*), sum(CASE WHEN enabled THEN 1 ELSE 0 END) FROM dim_keyword"
        ).fetchone()
    print(f"[extract] 写入 {written} 条；dim_keyword 现有 {total} 条（启用 {enabled}）"
          f" → {settings.paths.db}")
    return EXIT_OK


def _cmd_load(args: argparse.Namespace) -> int:
    """raw → DuckDB 的显式入口（与 parse-raw 相同实现，便于按契约模块名调用）。"""
    print("[load] 等价于 parse-raw（raw 目录重放入库）", file=sys.stderr)
    return _cmd_parse_raw(args)


def _cmd_qc(args: argparse.Namespace) -> int:
    """每日 QC + W1 验收四指标（只读，不发请求）。退出码 0=通过 / 22=未通过 / 1=错误。"""
    from . import qc

    settings = load_settings(args.settings)
    if args.db:
        settings = override_db_path(settings, args.db)
    report = qc.run_qc(settings, runs_dir=args.runs_dir,
                       window_days=args.window_days)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        for line in qc.format_report(report):
            print(line)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"[qc] 报告已写入：{target}")
    return EXIT_OK if report.ok else EXIT_QC_FAIL


def _cmd_session_gain(args: argparse.Namespace) -> int:
    """登录态增益实测（需可用登录态；无则如实 skipped，退出码 24）。"""
    from . import session_gain

    settings, tasks = load_config(args.settings, args.tasks)
    if args.db:
        settings = override_db_path(settings, args.db)
    task = tasks.by_id(args.task) if args.task else (
        tasks.enabled()[0] if tasks.enabled() else None)
    if task is None:
        print("[error] tasks.yaml 中没有可运行的任务", file=sys.stderr)
        return EXIT_ERROR
    report = session_gain.run_session_gain(settings, task=task, pages=args.pages,
                                           detail=args.detail, out_path=args.out)
    for line in session_gain.format_report(report):
        print(line)
    if report.status == session_gain.STATUS_SKIPPED:
        return EXIT_NO_LOGIN
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    """本地采集网关（前端 B 插件的数据入口，仅绑定 127.0.0.1）。阻塞运行，Ctrl+C 退出。"""
    from . import gateway

    settings, tasks = load_config(args.settings, args.tasks)
    if args.db:
        settings = override_db_path(settings, args.db)
    task = tasks.by_id(args.task) if args.task else (
        tasks.enabled()[0] if tasks.enabled() else None)
    if task is None:
        print("[error] tasks.yaml 中没有可用的 enabled 任务", file=sys.stderr)
        return EXIT_ERROR
    for message in all_warnings(settings, tasks):
        print(f"[warn] {message}", file=sys.stderr)
    try:
        # 传入全部任务作为「采集目标」候选；--task 只决定默认目标（未勾选时生效）
        gateway.serve(settings, task=task, tasks=tasks.tasks, host=args.host, port=args.port,
                      log_file=args.log_file)
    except OSError as exc:
        print(f"[gateway] 端口 {args.port} 无法监听：{exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """打开采集看板（桌面软件用法）：网关未运行则后台拉起，然后开应用窗口。"""
    from . import desktop

    settings = load_settings(args.settings)
    if args.db:
        settings = override_db_path(settings, args.db)
    try:
        result = desktop.launch_dashboard(settings, host=args.host, port=args.port)
    except desktop.LoopbackOnlyError as exc:
        print(f"[dashboard] {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not result.get("ok"):
        print(f"[dashboard] 打开失败：{result.get('error')}", file=sys.stderr)
        return EXIT_ERROR
    mode = "应用窗口" if result.get("opened") == "app-window" else "默认浏览器"
    started = "（已自动拉起后台服务）" if result.get("started_gateway") else ""
    print(f"[dashboard] 已在{mode}打开看板：{result['url']}{started}")
    return EXIT_OK


def _cmd_stop_gateway(args: argparse.Namespace) -> int:
    """优雅停止本机采集网关（后台服务）。"""
    from . import desktop

    try:
        result = desktop.stop_gateway(host=args.host, port=args.port)
    except desktop.LoopbackOnlyError as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        return EXIT_ERROR
    if result.get("already_stopped"):
        print("[stop] 网关本来就没有在运行")
        return EXIT_OK
    if result.get("ok"):
        print("[stop] 采集网关已停止（再次使用：双击桌面「pxb7采集看板」或运行 run.py dashboard）")
        return EXIT_OK
    print(f"[stop] 停止失败：{result.get('error')}", file=sys.stderr)
    return EXIT_ERROR


def _cmd_install_shortcut(args: argparse.Namespace) -> int:
    """在桌面创建「pxb7采集看板」快捷方式（双击即用）。"""
    from . import desktop

    result = desktop.install_shortcut()
    if result.get("ok"):
        print("[shortcut] 已在桌面创建「pxb7采集看板」快捷方式；双击即可打开看板"
              "（服务未运行时自动后台拉起）")
        return EXIT_OK
    print(f"[shortcut] 创建失败：{result.get('error') or result.get('stderr')}", file=sys.stderr)
    return EXIT_ERROR


def _cmd_paths(args: argparse.Namespace) -> int:
    """查看/自定义采集数据存放路径（看板同一份覆写文件 config/paths_override.json）。"""
    from . import config as cfg

    settings = load_settings(args.settings)

    if args.reset:
        removed = cfg.clear_path_overrides()
        print(f"[paths] 已恢复默认路径（{'删除' if removed else '无需删除'}覆写文件）")
        settings = load_settings(args.settings)
    if args.set:
        overrides: dict[str, str] = {key: str(value)
                                     for key, value in cfg.load_path_overrides().items()}
        for pair in args.set:
            key, sep, value = pair.partition("=")
            if not sep or not key.strip() or not value.strip():
                print(f"[paths] --set 需要 key=绝对路径 形式：{pair!r}", file=sys.stderr)
                return EXIT_USAGE
            overrides[key.strip()] = value.strip()
        try:
            resolved = cfg.validate_path_overrides(overrides)
            prospective = cfg.apply_path_overrides(settings, resolved)
            cfg.prepare_data_dirs(prospective)
            payload = cfg.save_path_overrides({k: str(v) for k, v in resolved.items()})
        except cfg.PathOverrideError as exc:
            print(f"[paths] 设置失败：{exc}", file=sys.stderr)
            return EXIT_ERROR
        settings = load_settings(args.settings)
        print(f"[paths] 已保存自定义路径（覆写文件：{cfg.PATHS_OVERRIDE_FILE}）：")
        for key, value in sorted(payload.items()):
            print(f"  {key:<9} {value}")
        if not settings.paths.db.is_file():
            print("[paths] 提示：新数据库尚未初始化——运行 run.py init-db"
                  "（或经看板保存路径时自动初始化）")

    items = cfg.paths_summary(settings)
    if args.json:
        print(json.dumps({"ok": True, "override_file": str(cfg.PATHS_OVERRIDE_FILE),
                          "items": items}, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"数据存放位置（覆写文件：{cfg.PATHS_OVERRIDE_FILE}）")
    for item in items:
        mark = " [已自定义]" if item["customized"] else (
            "" if item["overridable"] else "（只读）")
        print(f"  {item['label']}{mark}")
        print(f"    {item['path']}")
    print("自定义：python run.py paths --set db=D:/pxb7-data/pxb7.duckdb"
          " --set raw_root=D:/pxb7-data/raw/pxb7")
    print("恢复默认：python run.py paths --reset"
          "（也可在看板「数据存放位置」里图形化设置；登录态/风控状态目录不可迁移）")
    return EXIT_OK


def _cmd_browser_extension(args: argparse.Namespace) -> int:
    """列出本机 Chromium 系浏览器（Edge/Chrome/夸克…）及其扩展管理页与更新步骤。

    只检测、不启动浏览器——打开扩展页的动作在「更新浏览器扩展.bat / 安装浏览器扩展.bat」
    里以固定字面量路径完成（本命令负责告诉你在哪个浏览器、点哪里）。"""
    from . import browsers
    from .config import PROJECT_ROOT

    found = browsers.discover_browsers()
    if args.browser != "all":
        found = [b for b in found if b.key == args.browser]
    if args.json:
        print(json.dumps({"ok": bool(found), "action": args.action,
                          "extension_dir": str(PROJECT_ROOT / "extension" / "pxb7-extension"),
                          "browsers": [b.as_dict() for b in found]},
                         ensure_ascii=False, indent=2))
        return EXIT_OK if found else EXIT_ERROR
    print(f"扩展目录：{PROJECT_ROOT / 'extension' / 'pxb7-extension'}")
    print()
    for line in browsers.guide_lines(args.action, found):
        print(line)
    return EXIT_OK if found else EXIT_ERROR


def _cmd_native_host(args: argparse.Namespace) -> int:
    """注册/注销/检查「扩展弹窗一键启停网关」的 Native Messaging 宿主（HKCU，免管理员）。"""
    from . import native_host as NH
    from .config import load_settings

    settings = load_settings(args.settings)
    if args.action == "install":
        result = NH.install(settings, ext_id=args.ext_id)
    elif args.action == "uninstall":
        result = NH.uninstall()
    else:
        result = NH.check(settings)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    if args.action == "install":
        print(f"[native-host] 宿主清单：{result['manifest']}")
        print(f"[native-host] 扩展 ID：{result['ext_id']}（{result['ext_id_source']}）")
        print("[native-host] 启动器：" + result["launcher"])
        print("[native-host] 注册表登记：")
        for label, status in result["registry"]:
            print(f"[native-host]   {label}: {'✓' if status is True else status}")
        print(f"[native-host] {result['note']}")
        print("[native-host] 若扩展页显示的 ID 与上面推导不一致，请用 "
              "--ext-id <ID> 重新执行安装。")
    elif args.action == "uninstall":
        print("[native-host] 注册表注销：")
        for label, status in result["registry"]:
            print(f"[native-host]   {label}: {'✓ 已删除' if status is True else status}")
        print(f"[native-host] 宿主清单删除：{'是' if result['manifest_removed'] else '本就不存在'}")
    else:
        print(f"[native-host] 宿主清单：{result['manifest_path']}"
              f"（{'存在' if result['manifest_exists'] else '缺失，请先 install'}）")
        for label, ok in result["registry"]:
            print(f"[native-host]   {label}: {'已登记且指向本清单 ✓' if ok else '未登记/不一致'}")
        print(f"[native-host] 网关：{'运行中' if result['gateway_running'] else '未运行'}")
    return EXIT_OK


def _cmd_prepare_analysis(args: argparse.Namespace) -> int:
    """分析就绪层：建/重建分析视图（docs/01 §4「分析层」），按需导出 CSV/Parquet + 数据字典。"""
    from . import analysis

    settings = load_settings(args.settings)
    if args.db:
        settings = override_db_path(settings, args.db)
    result = analysis.prepare_analysis(settings, min_cell_sample=args.min_cell_sample)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for line in analysis.format_report(result):
            print(line)
    if args.out:
        exported = analysis.export_views(settings, args.out, fmt=args.format,
                                         game_id=args.game,
                                         quality=result.get("quality"))
        print(f"[analysis] 已导出 {len(exported['views'])} 个视图 → {exported['out_dir']}"
              f"（{exported['format']}，共 {len(exported['files'])} 个文件）")
        print(f"[analysis] 数据字典（先读这个）：{exported['readme']}")
    else:
        print("[analysis] 提示：加 --out DIR 可把视图导出为 CSV/Parquet 并生成数据字典"
              "（例：run.py prepare-analysis --out data/analysis）")
    return EXIT_OK


def _cmd_export_csv(args: argparse.Namespace) -> int:
    """把采集数据导出成 CSV（一行一个 listing）：full=分析主表全列；game=该游戏「看板列」精选。"""
    from . import analysis

    settings = load_settings(args.settings)
    if args.db:
        settings = override_db_path(settings, args.db)
    analysis.prepare_analysis(settings)          # 先刷新分析视图/样本门槛，保证与库同口径
    if args.layout == "game":
        if not args.game:
            print("[csv] 看板列版式（--layout game）需要同时给 --game <GAME_ID>；"
                  f"已登记版式的游戏：{'、'.join(str(g) for g in sorted(analysis.CURATED_GAME_LAYOUTS))}",
                  file=sys.stderr)
            return EXIT_USAGE
        result = analysis.export_curated_csv(settings, args.out, game_id=args.game)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return EXIT_OK
        for line in analysis.export_curated_csv_lines(result):
            print(line)
        return EXIT_OK
    result = analysis.export_main_csv(settings, args.out, game_id=args.game)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    for line in analysis.export_main_csv_lines(result):
        print(line)
    return EXIT_OK


def _cmd_split_csv(args: argparse.Namespace) -> int:
    """把一份 pxb7-listings CSV 按游戏拆成多个表（每游戏一份 CSV，列与源文件一致）。"""
    from . import analysis

    result = analysis.split_csv_by_game(args.csv_in, out_dir=args.out_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    for line in analysis.split_csv_lines(result):
        print(line)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数定义
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python pxb7-price-monitor/run.py",
        description="螃蟹游戏服务网（pxb7）账号挂牌价格采集与监测",
        epilog="红线：不做协议逆向/直连加密 API/指纹伪造与代理池；请求必须过 URL 守卫。",
    )
    parser.add_argument("--version", action="version", version=f"pxb7-price-monitor {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    def add_common(p, *, with_db: bool = True) -> None:
        p.add_argument("--settings", metavar="PATH", help="settings.yaml 路径（按 CWD 解析）")
        p.add_argument("--tasks", metavar="PATH", help="tasks.yaml 路径（按 CWD 解析）")
        if with_db:
            p.add_argument("--db", metavar="PATH", help="数据库路径（按 CWD 解析；默认取 settings）")

    p_init = sub.add_parser("init-db", help="建库（幂等）：建表 + 列口径注释 + 预置 dim_game/dim_task/词表")
    add_common(p_init)
    p_init.add_argument("--refresh-tasks", action="store_true",
                        help="dim_task 用 INSERT OR REPLACE 强制对齐 tasks.yaml（默认不覆盖已有行）")
    p_init.set_defaults(func=_cmd_init_db)

    p_crawl = sub.add_parser("crawl", help="采集一轮：风控前置 → 列表/详情采集 → 解析 → 词表抽取 → 入库 → summary")
    add_common(p_crawl)
    p_crawl.add_argument("--task", metavar="TASK_ID", help="只采指定任务；缺省取第一个 enabled 任务")
    p_crawl.add_argument("--pages", type=int, metavar="N",
                         help="本轮页数（受 settings.rate_limit.max_pages_per_run=5 上限约束）")
    p_crawl.add_argument("--detail", type=int, metavar="N", default=0,
                         help="额外采集 N 个详情页（M5：正在浏览/收藏）")
    p_crawl.add_argument("--guest", action="store_true", help="强制游客态（collected_via=guest）")
    p_crawl.add_argument("--summary-json", metavar="PATH", help="summary.json 输出路径（键名照契约）")
    p_crawl.add_argument("--run-id", metavar="ID", help="自定义 run_id（默认自动生成）")
    p_crawl.set_defaults(func=_cmd_crawl)

    p_parse = sub.add_parser("parse-raw", help="离线重放 raw 目录：重新解析并入库（不发起请求）")
    add_common(p_parse)
    p_parse.add_argument("--task", metavar="TASK_ID", help="任务；缺省取第一个 enabled 任务")
    p_parse.add_argument("--date", metavar="YYYYMMDD",
                         help="raw 日期分区（分隔符会被忽略，2026-10-02 亦可）；缺省全部")
    p_parse.add_argument("--run-dir", metavar="PATH", help="直接指定某个 run 目录（优先于 --date）")
    p_parse.add_argument("--raw-root", metavar="PATH", help="raw 落盘根（按 CWD 解析；默认取 settings）")
    p_parse.add_argument("--infer-delist", action="store_true",
                         help="重放时也推断下架（默认关闭：重放不代表当前在售状态）")
    p_parse.set_defaults(func=_cmd_parse_raw)

    p_status = sub.add_parser("status", help="打印 DB 计数、词表分布与 risk_state")
    p_status.add_argument("--db", metavar="PATH", help="数据库路径（按 CWD 解析）")
    p_status.add_argument("--settings", metavar="PATH", help="settings.yaml 路径（按 CWD 解析）")
    p_status.add_argument("--task", metavar="TASK_ID",
                          help="聚焦任务：顶层风控状态按该任务的档位显示（缺省显示全局镜像）")
    p_status.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_status.set_defaults(func=_cmd_status)

    p_login = sub.add_parser("login", help="打开有头浏览器人工登录，保存主/备登录态（单次尝试，不重试）")
    p_login.add_argument("--slot", choices=("primary", "backup"), default="primary",
                         help="写入主登录态还是备登录态（docs/01 §3.1-2 主备双号冗余）")
    p_login.add_argument("--auto-save", action="store_true",
                         help="轮询检测登录 Cookie（token+userId）后自动保存（无需终端回车；"
                              "适合从自动化流程里发起的人工登录）")
    p_login.add_argument("--timeout", type=int, default=300, metavar="秒",
                         help="--auto-save 的等待上限（默认 300 秒）")
    p_login.add_argument("--settings", metavar="PATH", help="settings.yaml 路径（按 CWD 解析）")
    p_login.set_defaults(func=_cmd_login)

    p_extract = sub.add_parser("extract", help="词表种子入 dim_keyword（幂等）：config/keywords_seed.yaml")
    add_common(p_extract)
    p_extract.add_argument("--dry-run", action="store_true", help="只统计不写库")
    p_extract.set_defaults(func=_cmd_extract)

    p_load = sub.add_parser("load", help="raw 目录 → DuckDB（parse-raw 的等价入口）")
    add_common(p_load)
    p_load.add_argument("--task", metavar="TASK_ID", help="任务；缺省取第一个 enabled 任务")
    p_load.add_argument("--date", metavar="YYYYMMDD", help="raw 日期分区（分隔符忽略）；缺省全部")
    p_load.add_argument("--run-dir", metavar="PATH", help="直接指定某个 run 目录（优先于 --date）")
    p_load.add_argument("--raw-root", metavar="PATH", help="raw 落盘根（按 CWD 解析）")
    p_load.add_argument("--infer-delist", action="store_true", help="重放时也推断下架（默认关闭）")
    p_load.set_defaults(func=_cmd_load)

    p_qc = sub.add_parser("qc", help="每日 QC（价格非负/断档≤36h/解析率/缺失率/去重）+ W1 验收四指标")
    p_qc.add_argument("--db", metavar="PATH", help="数据库路径（按 CWD 解析）")
    p_qc.add_argument("--settings", metavar="PATH", help="settings.yaml 路径（按 CWD 解析）")
    p_qc.add_argument("--runs-dir", metavar="PATH",
                      help="轮次 summary 目录（缺省扫 raw 根 + data/runs）")
    p_qc.add_argument("--window-days", type=int, default=3, help="W1 验收窗口天数（默认 3）")
    p_qc.add_argument("--out", metavar="PATH", help="QC 报告 JSON 输出路径")
    p_qc.add_argument("--json", action="store_true", help="以 JSON 打印")
    p_qc.set_defaults(func=_cmd_qc)

    p_gain = sub.add_parser("session-gain", help="登录态增益实测（单页条数/正在浏览完整值/收藏可见性）")
    add_common(p_gain)
    p_gain.add_argument("--task", metavar="TASK_ID", help="任务；缺省取第一个 enabled 任务")
    p_gain.add_argument("--pages", type=int, default=1, help="每种姿态采集页数（默认 1，≤5）")
    p_gain.add_argument("--detail", type=int, default=1, help="每种姿态的详情页数（默认 1）")
    p_gain.add_argument("--out", metavar="PATH", help="报告 JSON 输出路径")
    p_gain.set_defaults(func=_cmd_session_gain)

    p_serve = sub.add_parser(
        "serve", help="本地采集网关（前端 B 插件数据入口）：接收用户脚本推送的页面 DOM 并入库")
    add_common(p_serve)
    p_serve.add_argument("--task", metavar="TASK_ID", help="任务；缺省取第一个 enabled 任务")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认且建议 127.0.0.1 回环）")
    p_serve.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p_serve.add_argument("--log-file", metavar="PATH",
                         help="输出重定向到文件（分离进程/无控制台场景，如后台自启）")
    p_serve.set_defaults(func=_cmd_serve)

    p_dash = sub.add_parser("dashboard", help="打开采集看板（桌面窗口）：服务未运行则自动后台拉起")
    p_dash.add_argument("--host", default="127.0.0.1", help="网关地址（仅允许本机回环）")
    p_dash.add_argument("--port", type=int, default=8765, help="网关端口")
    p_dash.add_argument("--settings", metavar="PATH", help="settings.yaml 路径（按 CWD 解析）")
    p_dash.add_argument("--db", metavar="PATH", help="数据库路径（按 CWD 解析）")
    p_dash.set_defaults(func=_cmd_dashboard)

    p_stop = sub.add_parser("stop-gateway", help="停止后台的采集网关（优雅停止）")
    p_stop.add_argument("--host", default="127.0.0.1", help="网关地址（仅允许本机回环）")
    p_stop.add_argument("--port", type=int, default=8765, help="网关端口")
    p_stop.set_defaults(func=_cmd_stop_gateway)

    p_link = sub.add_parser("install-shortcut", help="在桌面创建「pxb7采集看板」快捷方式（双击即用）")
    p_link.set_defaults(func=_cmd_install_shortcut)

    p_paths = sub.add_parser(
        "paths", help="查看/自定义采集数据存放路径（与看板同一份覆写文件，CLI/网关同时生效）")
    p_paths.add_argument("--settings", metavar="PATH", help="settings.yaml 路径（按 CWD 解析）")
    p_paths.add_argument("--set", action="append", metavar="KEY=PATH", default=None,
                         help="设置自定义路径（可重复）：db / raw_root / runs / log_dir")
    p_paths.add_argument("--reset", action="store_true", help="清除全部自定义，恢复默认路径")
    p_paths.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_paths.set_defaults(func=_cmd_paths)

    p_ext = sub.add_parser(
        "browser-extension",
        help="列出本机 Edge/Chrome/夸克 等浏览器的扩展管理页与更新/安装步骤（只检测，不启动浏览器）")
    p_ext.add_argument("--action", choices=("update", "install"), default="update",
                       help="update=代码更新后「重新加载」；install=首次「加载已解压的扩展程序」")
    p_ext.add_argument("--browser", default="all",
                       help="只看某个浏览器（chrome/edge/quark/brave/vivaldi/qq…；默认全部）")
    p_ext.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_ext.set_defaults(func=_cmd_browser_extension)

    p_nh = sub.add_parser(
        "native-host",
        help="注册/注销/检查「扩展弹窗一键启停网关」的 Native Messaging 宿主（HKCU，免管理员）")
    add_common(p_nh)
    p_nh.add_argument("--action", choices=("install", "uninstall", "check"), default="install",
                      help="install=写清单并登记注册表（幂等）；uninstall=注销；check=只读检查")
    p_nh.add_argument("--ext-id", dest="ext_id", metavar="EXT_ID",
                      help="扩展 ID（32 位 a–p，扩展管理页可复制）；缺省按扩展目录路径自动推导")
    p_nh.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_nh.set_defaults(func=_cmd_native_host)

    p_an = sub.add_parser(
        "prepare-analysis",
        help="分析就绪层：建/重建分析视图（每号最新态/词表命中/价格带/周聚合…），"
             "可导出 CSV+Parquet 与数据字典")
    add_common(p_an)
    p_an.add_argument("--out", metavar="DIR", help="导出目录（缺省只建视图并打印质量摘要）")
    p_an.add_argument("--format", choices=("csv", "parquet", "both"), default="both",
                      help="导出格式（默认 both：CSV 便于肉眼，Parquet 便于 pandas/duckdb）")
    p_an.add_argument("--game", type=int, metavar="GAME_ID",
                      help="只导出该游戏的行（作用于含 game_id 的视图）")
    p_an.add_argument("--min-cell-sample", type=int, metavar="N",
                      help="样本门槛（默认取 settings.quality.min_cell_sample，通常 30）")
    p_an.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    p_an.set_defaults(func=_cmd_prepare_analysis)

    p_csv = sub.add_parser(
        "export-csv",
        help="把采集数据导出成一份分析用 CSV（一行一个 listing：最新价 + 结构化字段 + 词表命中 + 质量标记）")
    add_common(p_csv)
    p_csv.add_argument("--out", metavar="PATH",
                       help="输出路径（默认 data/analysis/pxb7-listings-<日期>.csv）")
    p_csv.add_argument("--game", type=int, metavar="GAME_ID", help="只导出该游戏")
    p_csv.add_argument("--layout", choices=("full", "game"), default="full",
                       help="full=分析主表全列（默认）；game=该游戏「看板列」精选版式"
                            "（需 --game；列名用该游戏自己的词表说法，如鸣潮的"
                            "共鸣链（N命）/武器精炼（精N）/资源/额外付费商品）")
    p_csv.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    p_csv.set_defaults(func=_cmd_export_csv)

    p_split = sub.add_parser(
        "split-csv",
        help="把 pxb7-listings CSV 按游戏拆成多个表（每游戏一份 CSV，列与源文件一致，Excel 友好）")
    p_split.add_argument("--in", dest="csv_in", metavar="PATH", required=True,
                         help="要拆分的 CSV 路径（如 data/analysis/pxb7-listings-20261003.csv）")
    p_split.add_argument("--out-dir", metavar="DIR",
                         help="输出目录（默认源文件同级 by_game/）")
    p_split.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    p_split.set_defaults(func=_cmd_split_csv)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyError as exc:                     # 未知 task_id 等
        print(f"[config] 未知标识：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"[io] {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        from .pipeline import PipelineError
        if isinstance(exc, PipelineError):
            print(f"[run] {exc}", file=sys.stderr)
            return EXIT_ERROR
        raise
    except KeyboardInterrupt:
        print("[abort] 用户中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
