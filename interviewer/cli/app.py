"""命令行入口：transport 收口层。四个职责：进件护栏交互环（判定规则单点
在 resume/intake_guard）；续面与驱动（run_new_session / run_resumed_session，
续面只发 Command(resume) 绝不重传初始 state——纪律由 driver 签名保证）；
交互回调（_handle_interrupt 是 driver 的 on_interrupt，本层唯一 I/O）；
会话收割落盘（transcript/report 写文件，I/O 不进图）。

main() 只做装配与分发：_select_session 四模式分发（--list/--continue/
--resume/新场）+ _intake 三环进件收集 + 驱动 + 收割；裁决收敛在 _resolve
单点，三环同为 fail-visible。续面三件套语义同 aider / Claude Code / Codex
CLI：状态权威在 checkpointer（thread_id 定位 → get_state → Command(resume)）。
"""
import argparse
import sys
import uuid

from interviewer.agents.utils.agent_states import RESUME_ANSWER
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph, graph_signature
from interviewer.graph.session_driver import (
    NoPendingInterrupt,
    run_new_session,
    run_resumed_session,
)
from interviewer.graph.session_runner import create_initial_state, persist_session
from interviewer.resume.file_loader import (
    DEFAULT_MAX_CHARS,
    ResumeFileError,
    SUPPORTED_EXTS,
    load_resume_file,
)
from interviewer.resume.intake_guard import (
    IntakeVerdict,
    check_jd,
    check_position,
    check_resume_text,
    semantic_guard,
)
from interviewer.resume.jd_parser import parse_position_input
from interviewer.resume.parser import candidate_profile_text, parse_pasted_resume


class _PlainConsole:
    """rich 不可用时的最小降级（保持 CLI 可跑）。"""

    def print(self, *args, **kwargs):
        print(*args)


def _make_console():
    try:
        from rich.console import Console

        return Console()
    except ImportError:
        return _PlainConsole()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m interviewer.cli.app", description="陪面 CLI（模拟面试）")
    ap.add_argument("--resume", metavar="SESSION_ID",
                    help="续面指定会话（ID 见启动横幅或 --list；新会话直接不带参数启动）")
    ap.add_argument("--continue", dest="continue_latest", action="store_true",
                    help="续最近一场未完成会话")
    ap.add_argument("--list", action="store_true", help="列出当前图版本的全部会话后退出")
    return ap.parse_args(argv)


def session_rows(saver, graph, signature: str) -> list[dict]:
    """当前图签名下的全部会话（--list/--continue/--resume 的共同数据源）。
    thread_id 里的签名不匹配 = 图形状已变的失联会话，当前图续不了，不列。

    枚举走 saver 公开 list 接口，不裸查 SqliteSaver 表结构（表名/列名是
    implementation 不是 interface）。排序自己按每 thread 的最大
    checkpoint_id 降序（checkpoint_id 是 uuid6 时间序），不依赖各家 saver
    的列举顺序，与旧 SQL 的 GROUP BY + ORDER BY 逐位等价。代价：list 反
    序列化全部 blob——个人规模无感，且 --list 不在面试热路径。
    """
    suffix = f"::{signature}"
    latest: dict[str, str] = {}
    for tup in saver.list(None):
        tid = tup.config["configurable"]["thread_id"]
        if not tid.endswith(suffix):
            continue                  # 失联会话（旧图签名）不列
        cid = tup.config["configurable"].get("checkpoint_id") or ""
        if cid >= latest.get(tid, ""):
            latest[tid] = cid
    rows = []
    for tid in sorted(latest, key=latest.get, reverse=True):
        snap = graph.get_state({"configurable": {"thread_id": tid}})
        values = snap.values or {}
        rows.append({
            "session_id": tid[: -len(suffix)],
            "ongoing": bool(snap.next),      # 挂起中断 = 有待答问题
            "position": values.get("position") or "?",
            "asked": len(values.get("issued_keys") or []),
        })
    return rows


def _format_session_rows(rows: list[dict]) -> str:
    lines = [f"  {r['session_id']}｜{'进行中' if r['ongoing'] else '已结束'}"
             f"｜{r['position']}｜已问 {r['asked']} 题" for r in rows]
    return "\n".join(lines)


def _load_resume_input(console, config: dict) -> str:
    """简历输入环（2026-09-18 文件上传版）：路径 → load_resume_file，失败人话重输。

    失败必须重输而不是静默跳过：用户给了文件就得给反馈（fail-loud）；
    "不要简历"必须是用户自己按的回车（fail-open 由用户选择，不是系统替他选）。
    """
    max_chars = config.get("resume_max_chars", DEFAULT_MAX_CHARS)
    while True:
        raw = input(f"简历文件路径（支持 {SUPPORTED_EXTS}；回车跳过）> ").strip()
        if not raw:
            return ""
        try:
            text = load_resume_file(raw, max_chars=max_chars)
        except ResumeFileError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        console.print(f"[green]已读取简历（{len(text)} 字）[/green]")
        return text


def _handle_interrupt(console, value: dict):
    """把 interrupt 载荷转成用户交互，返回 Command(resume=...) 的载荷；:exit 返回 None。

    动态架构下只剩一类挂起（提问/自我介绍共用 kind="question"）；
    批式时代的 plan_review 过目分支已随 Planner 退役（2026-09-17 WP3）。
    """
    console.print(f"\n[bold cyan]面试官[/bold cyan]：{value.get('question', '')}")
    raw = input("\n你的回答（:skip 跳过 / :exit 退出）> ").strip()
    if raw == ":exit":
        return None
    if raw == ":skip":
        return {"type": "skip"}
    return {"type": RESUME_ANSWER, "text": raw}


def _confirm(console, reasons: list[str], what: str) -> bool:
    """灰区确认环：大声提醒 + 决定权留给用户（y=继续，其他=重新输入）。"""
    console.print(f"[yellow]{what}存疑：{'；'.join(reasons)}[/yellow]")
    return input("仍要用它继续吗？(y=继续 / 其他=重新输入) > ").strip().lower() == "y"


def _resolve(console, verdict: IntakeVerdict, what: str) -> bool:
    """三环共用的裁决收敛：True=放行（干净通过或用户坚持），False=打回重输。
    not ok → 红字原因；needs_confirm → 灰区确认。三环同为 fail-visible。"""
    if not verdict.ok:
        console.print(f"[red]{what}输入无效：{'；'.join(verdict.reasons)}[/red]")
        return False
    if verdict.needs_confirm:
        return _confirm(console, verdict.reasons, what)
    return True


def _semantic_check(console, config: dict, position: str, jd: str, resume: str) -> bool:
    """LLM 语义软判（进件护栏的语义层）。返回 False = 用户放弃（终止流程）。

    失败语义 fail-open：LLM 不可用/回复不可解析 → 提示后放行——进件提醒是
    增强件，缺席不许挡面试（fail-open 的落点在进件提醒，不在面试流程）。
    """
    try:
        from interviewer.llm import create_llm_client

        llm = create_llm_client("quick", config)
        verdict = semantic_guard(llm, position, jd, resume)
    except Exception:
        console.print("[yellow]进件语义核验不可用（已跳过）[/yellow]")
        return True
    if verdict.needs_confirm:
        console.print(f"[yellow]语义核验：{'；'.join(verdict.reasons)}[/yellow]")
        if input("仍要开始这场面试吗？(y=开始 / 其他=终止) > ").strip().lower() != "y":
            return False
    return True


def _intake(console, config: dict) -> tuple[str, str, str] | None:
    """新场进件三环 + 语义核验。三环同形、差异显式保留：岗位必填（agent
    不替用户推断岗位）；JD/简历可回车跳过——跳过是用户选择，不进判定。
    裁决统一走 _resolve。返回 (岗位, JD 要点, 简历画像)，None = 用户在
    语义核验处终止（终止提示由本函数打印）。两层分工见 intake_guard 模块头。
    """
    while True:
        position = input("请输入本次面试的岗位（必填）> ").strip()
        if not position:
            console.print("[red]岗位不能为空：agent 不会替你推断岗位[/red]")
            continue
        if _resolve(console, check_position(position), "岗位"):
            break

    jd_raw = ""
    while True:
        raw = input("可选：粘贴目标岗位的 JD 要点（回车跳过）> ").strip()
        if not raw:
            break                          # 跳过 JD = 用户自己的选择，不判
        if _resolve(console, check_jd(raw), "JD"):
            jd_raw = raw
            break

    while True:
        resume_raw = _load_resume_input(console, config)
        if not resume_raw:
            break                          # 跳过简历 = 用户自己的选择，不判
        if _resolve(console, check_resume_text(resume_raw), "简历内容"):
            break
        # 被打回 → 回文件选择环重挑（红字原因已由 _resolve 打印）

    if not _semantic_check(console, config, position, jd_raw, resume_raw):
        console.print("[yellow]已终止。检查进件材料后重新启动即可。[/yellow]")
        return None

    parsed = parse_position_input(position, jd_raw)
    profile = candidate_profile_text(parse_pasted_resume(resume_raw))
    return parsed["position"], parsed["requirements"], profile


def _select_session(args, rows: list[dict], console) -> tuple[str, bool] | int:
    """四模式分发：--list/--continue/--resume/新场。返回 (session_id,
    resume_mode)；int = 退出码（清单已列/已收场提示 = 0；找不到可续会话 = 1）。
    fail-loud：ID 打错/旧图版本失联都明确说，绝不静默开新场——静默新场 =
    用户以为在续面，实际旧场永久丢失。
    """
    if args.list:
        if not rows:
            print("还没有任何会话（当前图版本）")
        else:
            print(f"当前图版本共 {len(rows)} 场会话（新→旧）：")
            print(_format_session_rows(rows))
        return 0

    if args.continue_latest:
        ongoing = [r for r in rows if r["ongoing"]]
        if not ongoing:
            print("没有未完成会话（--list 查看全部）", file=sys.stderr)
            return 1
        session_id = ongoing[0]["session_id"]
        console.print(f"[yellow]--continue：续最近一场未完成会话 {session_id}[/yellow]")
        return session_id, True

    if args.resume:
        matched = next((r for r in rows if r["session_id"] == args.resume), None)
        if matched is None:
            print(f"会话 {args.resume} 不存在，或属于旧图版本（当前图不可续）"
                  f"——--list 查看可续会话", file=sys.stderr)
            return 1
        if not matched["ongoing"]:
            print(f"会话 {args.resume} 已收场；记录与报告在 data/sessions/ 下同名文件")
            return 0
        console.print(f"[yellow]--resume：续面会话 {args.resume}[/yellow]")
        return args.resume, True

    ongoing_n = sum(1 for r in rows if r["ongoing"])
    if ongoing_n:
        console.print(f"[yellow]检测到 {ongoing_n} 场未完成会话——"
                      f"--continue 续最近一场，--list 查看全部[/yellow]")
    return uuid.uuid4().hex[:12], False


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = _parse_args(argv)
    console = _make_console()
    config = _load_config()
    signature = graph_signature(config)

    saver, close_checkpointer = build_checkpointer(config)
    try:
        coach = CoachGraph(config, checkpointer=saver)
        graph = coach.build()

        sel = _select_session(args, session_rows(saver, graph, signature), console)
        if isinstance(sel, int):
            return sel                    # 分发即终局（清单已列/已收场/无可续）
        session_id, resume_mode = sel

        cfg = coach.invoke_config(session_id)

        def on_interrupt(value: dict):
            return _handle_interrupt(console, value)

        if resume_mode:
            # 续面：只发 Command(resume)，绝不重传初始 state
            # 纪律是类型事实：run_resumed_session 的签名里没有 initial_state
            # 参数，想违反都违反不了（driver 合同见 graph/session_driver.py）。
            try:
                result = run_resumed_session(graph, cfg, on_interrupt)
            except NoPendingInterrupt:
                print(f"会话 {session_id} 没有挂起的问题（可能刚被其他进程续完）",
                      file=sys.stderr)
                return 1
        else:
            collected = _intake(console, config)
            if collected is None:         # 用户在语义核验处终止（_intake 已打印）
                return 0
            position, requirements, profile = collected
            payload = create_initial_state(session_id, position, profile, requirements)
            console.print(f"[green]本场会话 ID：{session_id}｜中途退出后 "
                          f"python -m interviewer.cli.app --resume {session_id} 可继续[/green]")
            result = run_new_session(graph, cfg, payload, on_interrupt)

        if not result.finished:
            # 用户中途退出：进度已在 checkpointer，收割落盘不做
            #（未收场的会话由 --resume 续完后再落盘）
            console.print(f"[yellow]已退出。会话 {session_id} 已存档，"
                          f"--resume {session_id} 继续（--list 查看）[/yellow]")
            return 0

        final_state = result.final_values
        paths = persist_session(config, final_state)
        console.print(f"\n[green]面试结束。记录已保存：{paths['session']}[/green]")
        if paths.get("report"):
            console.print(f"[green]终场报告：{paths['report']}[/green]")
            try:
                from rich.markdown import Markdown

                console.print(Markdown(final_state.get("interview_report", "")))
            except ImportError:
                console.print(final_state.get("interview_report", ""))
        return 0
    finally:
        close_checkpointer()


def _load_config() -> dict:
    from interviewer.default_config import get_config

    return get_config()


if __name__ == "__main__":
    sys.exit(main())
