"""Render a compact, offline Codex acceptance report from a private acceptance record.

Only a narrow allowlist of values is published; the acceptance record itself can
contain machine paths, terminal output, task identifiers and other private data.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from string import Template

SOURCE_PATHS = (
    "scripts/smoke_hc.py",
    "scripts/bridge_protocol.json",
    # Historical reports retain their original development-document identities.
    "README.md",
    "AGENTS.md",
    "docs/ROADMAP.md",
    "docs/decisions/0001-herdr-core-first.md",
    "src/herdr_agent_bridge/tools/smoke_hc.py",
    "tests/test_smoke_hc.py",
    "docs/PROJECT.md",
    "src/herdr_agent_bridge/resources/bridge_protocol.json",
)
CHECK_GROUPS = (
    (
        "只读任务与证据",
        "启动、等待、返回结果及对源码引用的独立核对。",
        (
            "interactive_agent_started",
            "wait_timeout_keeps_original_task",
            "first_result_independently_verified",
        ),
    ),
    (
        "追问与投递核对",
        "回执丢失、拒绝重发、原会话追问关联及发送次数。",
        (
            "lost_reply_records_unknown_before_reconciliation",
            "refuses_replay_followup",
            "refuses_replay_replacement",
            "followup_and_delivery_reconciliation_verified",
            "four_tasks_exactly_four_send_attempts",
        ),
    ),
    (
        "业务提问与继续",
        "Agent 提问、脚本模拟答复及后续结果的关联。",
        (
            "real_agent_returns_question_for_human",
            "human_answer_continues_task",
        ),
    ),
    (
        "控制情形与源码",
        "退出、未知、阻塞、启动失败及源码未改变检查。",
        (
            "control_exited",
            "control_unknown",
            "control_blocked",
            "control_startup_failure",
            "six_task_source_files_unchanged",
        ),
    ),
)
INJECTION = (
    "One actual send reply is discarded locally after hc returns; not a real network outage."
)
NOT_VERIFIED = {
    "Grok Bot": "Grok Bot",
    "Herdr App": "Herdr App",
    "real permission/trust dialog": "真实权限／信任弹窗",
}


def esc(value):
    """Escape every data-derived text or attribute before insertion in HTML."""
    return html.escape(str(value), quote=True)


def check_index(report):
    checks = report.get("checks")
    index = {}
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict) and isinstance(item.get("check"), str):
                index.setdefault(item["check"], []).append(item)
    return index


def state(index, names):
    items = [item for name in names for item in index.get(name, [])]
    if any(item.get("passed") is False for item in items):
        return "fail"
    if all(index.get(name) for name in names) and all(item.get("passed") is True for item in items):
        return "pass"
    return "missing"


def badge(status):
    label = {"pass": "已核对", "fail": "未通过", "missing": "未记录／未完成"}[status]
    return '<span class="badge ' + esc(status) + '">' + esc(label) + "</span>"


def cleanup_state(report):
    cleanup = report.get("cleanup")
    if not isinstance(cleanup, list) or not cleanup:
        return "missing"
    if any(not isinstance(item, dict) or item.get("passed") is False for item in cleanup):
        return "fail"
    if not all(item.get("passed") is True for item in cleanup):
        return "missing"
    # Match owned terminal evidence, not record counts. Do not echo their args.
    terminals = {"kill": set(), "rm": set(), "get": set()}
    server_stopped = False
    for item in cleanup:
        args = item.get("args")
        if not (
            isinstance(args, list)
            and len(args) == 2
            and all(isinstance(arg, str) and arg for arg in args)
        ):
            continue
        if args == ["server", "stop"]:
            server_stopped = True
        elif args[0] in terminals:
            if args[0] != "get" or item.get("reconciled") == "absent":
                terminals[args[0]].add(args[1])
    if server_stopped and (terminals["kill"] & terminals["rm"] or terminals["get"]):
        return "pass"
    return "missing"


def clean_version(value, command):
    if not isinstance(value, str):
        return "未记录"
    match = re.fullmatch(
        r"(?:" + command + r")\s+(v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)", value.strip()
    )
    return match.group(1) if match else "未记录或格式不受支持"


def run_label(value):
    if isinstance(value, str) and re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", value):
        return value
    return "未记录或格式不受支持"


def result_counts(report):
    results = report.get("results")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        return "任务结果明细未记录。"
    total = len(results)
    received = sum(item.get("delivery") == "received" for item in results)
    verified = sum(item.get("verified_success") is True for item in results)
    human = sum(item.get("observed") == "needs_human" for item in results)
    sentence = f"任务记录 {total} 项：已接收 {received} 项，独立核对结果 {verified} 项，业务提问 {human} 项（不计作结果成功）。"
    threads = [item.get("thread_id") for item in results]
    if total == 4 and all(isinstance(t, str) and t for t in threads) and len(set(threads)) == 1:
        sentence += "四轮记录属于同一 Codex 线程。"
    return sentence


def excerpt(report, index):
    """Publish only specific expected answers backed by independent checks."""
    results = report.get("results")
    if not isinstance(results, list):
        return ""
    answers = []
    for item in results:
        if not isinstance(item, dict) or item.get("verified_success") is not True:
            continue
        result = item.get("result")
        if isinstance(result, dict) and result.get("kind") == "result":
            answer = result.get("answer")
            if isinstance(answer, dict):
                answers.append(answer)
    parts = []
    if state(index, ("first_result_independently_verified",)) == "pass":
        for answer in answers:
            if answer.get("blocked_error_kind") == "blocked":
                parts.append("阻塞错误：<code>" + esc(answer["blocked_error_kind"]) + "</code>")
                break
    if state(index, ("followup_and_delivery_reconciliation_verified",)) == "pass":
        for answer in answers:
            if (
                answer.get("method_name") == "test_optimized_run_rejects_wrong_task_evidence"
                and answer.get("asserted_message") == "missing task evidence"
            ):
                parts.append("追问断言：<code>" + esc(answer["asserted_message"]) + "</code>")
                break
    return '<p class="excerpt">回答摘录：' + "；".join(parts) + "。</p>" if parts else ""


def source_hashes(report):
    hashes = report.get("source_sha256")
    if not isinstance(hashes, dict):
        return "<span>源码摘要未记录</span>"
    entries = []
    for path in SOURCE_PATHS:
        digest = hashes.get(path)
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            entries.append("<span><code>" + esc(path) + "</code> " + esc(digest[:12]) + "</span>")
    return " ".join(entries) if entries else "<span>源码摘要未记录或格式不受支持</span>"


def unverified(report):
    values = report.get("not_verified")
    if not isinstance(values, list):
        return "未验证范围未在记录中列出。"
    names = [label for key, label in NOT_VERIFIED.items() if key in values]
    if any(not isinstance(value, str) or value not in NOT_VERIFIED for value in values):
        names.append("其他未公开范围")
    return "未验证：" + ("、".join(names) if names else "记录中未列出项目") + "。"


def injection_text(report):
    if report.get("failure_injection") == INJECTION:
        return "故障注入是在 hc 返回后本地丢弃一次发送回执，并非真实网络故障。"
    return "故障注入描述未记录或不受支持；本地回执丢弃不等于网络故障。"


def render(report, acceptance_sha, regression_count, execution_context=None):
    index = check_index(report)
    groups = [
        (title, description, state(index, names)) for title, description, names in CHECK_GROUPS
    ]
    cleanup = cleanup_state(report)
    raw_checks = report.get("checks")
    clean_checks = (
        isinstance(raw_checks, list)
        and bool(raw_checks)
        and all(isinstance(item, dict) and item.get("passed") is True for item in raw_checks)
    )
    passed = (
        report.get("passed") is True
        and report.get("phase") == "finished"
        and clean_checks
        and cleanup == "pass"
        and all(status == "pass" for _, _, status in groups)
    )
    title = "本次验收通过" if passed else "本次验收未通过"
    lead = (
        "本地管理者通过 hc 专用会话完成真实 Codex 只读任务、追问、业务提问与预设答复的核对。"
        if passed
        else "记录显示失败、尚未完成或证据不完整；不能将本次运行视为通过。"
    )
    flow_final = "验收通过" if passed else "验收判定"
    cards = "".join(
        '<article class="card"><div class="card-title"><h3>'
        + esc(name)
        + "</h3>"
        + badge(status)
        + "</div><p>"
        + esc(description)
        + "</p></article>"
        for name, description, status in groups
    )
    versions = report.get("versions") if isinstance(report.get("versions"), dict) else {}
    hc_version = clean_version(versions.get("hc"), "hc")
    codex_version = clean_version(versions.get("codex"), "codex(?:-cli)?")
    regression = (
        f"；本地回归测试数量 {regression_count} 项（由渲染命令提供）"
        if regression_count is not None
        else ""
    )
    cleanup_text = {
        "pass": "专用会话及其服务的清理检查已记录",
        "fail": "清理检查未通过",
        "missing": "清理检查未记录或不完整",
    }[cleanup]
    context = (
        "运行环境说明（由渲染命令提供）：本次采用受控的外层提权；Codex 内层配置仍为 read-only。"
        if execution_context == "outer-elevated"
        else ""
    )
    values = {
        "status_class": "pass" if passed else "fail",
        "title": esc(title),
        "lead": esc(lead),
        "cards": cards,
        "counts": esc(result_counts(report)),
        "excerpt": excerpt(report, index),
        "cleanup": esc(cleanup_text),
        "regression": esc(regression),
        "flow_final": esc(flow_final),
        "injection": esc(injection_text(report)),
        "unverified": esc(unverified(report)),
        "hc_version": esc(hc_version),
        "codex_version": esc(codex_version),
        "run_id": esc(run_label(report.get("run_id"))),
        "acceptance_sha": esc(acceptance_sha),
        "sources": source_hashes(report),
        "context": "<p>" + esc(context) + "</p>" if context else "",
    }
    return PAGE.substitute(values)


PAGE = Template("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Codex 只读任务验收报告</title>
<style>
:root { color-scheme: light; font-family: system-ui, -apple-system, "Microsoft YaHei", sans-serif; color: #202b35; background: #f4f6f7; }
* { box-sizing: border-box; }
body { margin: 0; font-size: 15px; line-height: 1.5; }
main { max-width: 1160px; margin: auto; padding: 30px 28px 34px; }
h1, h2, h3, p, figure { margin: 0; }
h1 { font-size: clamp(25px, 3.4vw, 36px); line-height: 1.2; }
h2 { font-size: 19px; line-height: 1.3; }
h3 { font-size: 15px; }
header { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 18px; }
.kicker { color: #526675; font-size: 12px; font-weight: 700; letter-spacing: .08em; margin-bottom: 5px; }
.meta { color: #526675; font-size: 12px; text-align: right; }
.conclusion { background: #fff; border: 1px solid #dce3e6; border-left: 5px solid #338166; padding: 14px 18px; }
.conclusion.fail { border-left-color: #a5483d; }
.conclusion p { color: #465862; margin-top: 4px; }
.grid { display: grid; grid-template-columns: 1.1fr .9fr; gap: 20px; margin-top: 18px; }
.section { background: #fff; border: 1px solid #dce3e6; padding: 16px 18px; min-width: 0; }
.section > h2 { margin-bottom: 10px; }
.cards { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.card { border: 1px solid #e0e7e9; background: #fafbfb; padding: 10px; min-width: 0; }
.card-title { display: flex; align-items: start; justify-content: space-between; gap: 6px; }
.card p { color: #53636d; font-size: 13px; margin-top: 4px; }
.badge { flex: none; border-radius: 3px; font-size: 11px; font-weight: 700; padding: 2px 5px; }
.badge.pass { background: #e6f2eb; color: #246746; }
.badge.fail { background: #fae9e6; color: #963c32; }
.badge.missing { background: #fff2d9; color: #795616; }
.facts { margin-top: 10px; color: #465862; font-size: 13px; }
.excerpt { margin-top: 5px; color: #465862; font-size: 13px; }
.flow { margin-top: 13px; }
.flow figcaption { font-size: 13px; font-weight: 700; color: #526675; margin-bottom: 6px; }
.route { display: flex; gap: 14px; list-style: none; margin: 0; padding: 0; }
.route li { position: relative; flex: 1 1 0; min-width: 0; border: 1px solid #cedadd; border-radius: 3px; background: #f6f9f9; text-align: center; padding: 7px 4px; font-size: 12px; line-height: 1.35; }
.route li:not(:last-child)::after { content: '→'; position: absolute; right: -13px; top: 50%; transform: translateY(-50%); color: #607780; font-weight: bold; }
.branches { display: grid; grid-template-columns: 2fr 1fr; gap: 8px; margin-top: 8px; }
.branch { padding: 7px 9px; background: #fff8e9; border-left: 3px solid #d0a35b; font-size: 12px; }
.note { color: #53636d; font-size: 12px; margin-top: 7px; }
.boundaries { margin-top: 10px; font-size: 13px; color: #465862; }
.boundaries p + p { margin-top: 4px; }
.bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 18px; }
.next { padding-left: 20px; margin: 0; font-size: 13px; }
.next li + li { margin-top: 4px; }
pre { margin: 0; padding: 10px; background: #f5f7f8; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; line-height: 1.5; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.foot { margin-top: 14px; color: #64747d; font-size: 11px; }
.foot p + p { margin-top: 4px; }
.hash { overflow-wrap: anywhere; }
.sources { display: flex; flex-wrap: wrap; gap: 3px 14px; }
@media (max-width: 800px) {
  main { padding: 20px 15px; }
  header { align-items: start; flex-direction: column; gap: 8px; }
  .meta { text-align: left; }
  .grid, .bottom { grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }
}
@media (max-width: 520px) {
  .cards { grid-template-columns: 1fr; }
  .route { flex-direction: column; gap: 13px; }
  .route li:not(:last-child)::after { content: '↓'; top: auto; bottom: -15px; right: 50%; transform: translateX(50%); }
  .branches { grid-template-columns: 1fr; }
}
@media print {
  :root { background: #fff; }
  main { max-width: none; padding: 0; }
  .section, .conclusion, .card, .flow, .bottom { break-inside: avoid; }
  .grid, .bottom { gap: 10px; margin-top: 10px; }
  .section { padding: 10px; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; }
}
</style>
</head>
<body><main>
<header><div><p class="kicker">HERDR AGENT BRIDGE / CODEX</p><h1>只读任务验收报告</h1></div>
<p class="meta">hc $hc_version · Codex CLI $codex_version<br>运行记录 $run_id</p></header>
<section class="conclusion $status_class"><h2>$title</h2><p>$lead</p></section>
<div class="grid">
<section class="section"><h2>关键结果</h2><div class="cards">$cards</div>
<p class="facts">$counts<br>$cleanup$regression。</p>$excerpt</section>
<section class="section"><h2>链路与判断</h2>
<figure class="flow"><figcaption>本地链路</figcaption><ol class="route"><li>本地管理者</li><li>bridge<br>journal</li><li>hc<br>专用会话</li><li>交互式<br>Codex</li></ol></figure>
<figure class="flow"><figcaption>正常路径（示意）</figcaption><ol class="route"><li>输入已提交</li><li>已接收</li><li>返回证据</li><li>独立核对</li><li>$flow_final</li></ol></figure>
<div class="branches"><div class="branch">等待超时 → 继续观察原任务<br>投递未知 → 原会话核对<br><strong>未核对前禁止重发</strong></div><div class="branch">blocked → 人工处理</div></div>
<p class="note">终端状态本身不构成验收；仅在独立核对满足条件时判定通过。</p></section>
</div>
<div class="bottom">
<section class="section"><h2>验收范围与边界</h2>
<div class="boundaries"><p>$injection</p><p>$unverified 本地 fixture 不能证明 Grok 集成或真实 Agent 权限界面；业务提问由脚本模拟答复，并非权限弹窗。</p>
<p>read-only 要求本机环境允许 Codex 启动其自身沙箱；只读配置与专用会话的状态隔离不构成同账号进程间的安全隔离。</p>$context</div>
</section>
<section class="section"><h2>复现</h2><pre><code>python3 -m unittest discover -s tests -v
herdr-bridge tool accept_codex
herdr-bridge tool render_codex_report .artifacts/acceptance/codex/&lt;run-id&gt;/acceptance.json \\
  --output .artifacts/acceptance/codex/report.html --regression-count &lt;回归数&gt;</code></pre>
<p class="note">替换占位符；真实验收需要 hc、Codex CLI 和可用认证，会调用真实模型。回归数量由调用方提供，并非验收 JSON 的证明。</p></section>
</div>
<footer class="foot"><p class="hash">验收 JSON SHA-256：<code>$acceptance_sha</code></p>
<p>源码 SHA-256 前 12 位（完整摘要保存在私有验收记录中）：</p><div class="sources">$sources</div></footer>
</main></body></html>
""")


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("acceptance_json", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--regression-count", type=nonnegative)
    parser.add_argument(
        "--execution-context",
        choices=["outer-elevated"],
        help="execution context supplied by the operator, not present in the acceptance JSON",
    )
    args = parser.parse_args(argv)
    try:
        raw = args.acceptance_json.read_bytes()
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise ValueError("acceptance JSON must contain an object")
        rendered = render(
            report, hashlib.sha256(raw).hexdigest(), args.regression_count, args.execution_context
        )
        args.output.write_text(rendered, encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Cannot render acceptance report: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
