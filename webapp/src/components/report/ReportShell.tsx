"use client";

/**
 * 终场报告壳：挂载 GET /sessions/[id]。
 * finished → 报告（圆环头 / 总评 markdown / 逐题表 / 薄弱点回链 / 操作）；
 * 未结束 → 引导回面试页；失败 → 原位重试。数据全部来自 final 载荷，无硬编码分数。
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import Markdown from "react-markdown";
import { ApiError, getSession } from "@/lib/api";
import type { FinalPayload, SessionState } from "@/lib/types";
import ScoreRing from "./ScoreRing";
import QTable from "./QTable";
import { dimAverages, gradeOf, weakPoints } from "./reportLogic";
import s from "./report.module.css";

type Phase = "loading" | "error" | "ready";

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return "报告加载失败，请重试";
}

function headlineOf(overall: number): string {
  if (overall >= 4.5) return "整场发挥出色，保持这个节奏";
  if (overall >= 3.5) return "整体扎实，把薄弱点补上还能再进一步";
  if (overall >= 2.5) return "基础在，细节和深度还有明显空间";
  return "先把基础考点答稳，再追求深度";
}

function TitleIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="3" width="16" height="18" rx="2" />
      <path d="M8 8h8M8 12h8M8 16h5" />
    </svg>
  );
}

function GridIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 6h11M9 12h11M9 18h11" />
      <path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01" />
    </svg>
  );
}

function TargetIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1" />
    </svg>
  );
}

function ReportBody({ session, final }: { session: SessionState; final: FinalPayload }) {
  const results = final.topic_results;
  const averages = dimAverages(results);
  const weak = weakPoints(results);
  const mainCount = results.filter((r) => !r.is_followup).length;
  const followupCount = results.length - mainCount;

  return (
    <div className={`shell ${s.rcol}`}>
      <div className="page-head">
        <h1>面试报告</h1>
        <p>
          {session.position} · {mainCount} 题{followupCount > 0 ? ` · 含 ${followupCount} 次追问` : ""}
        </p>
      </div>

      <div className={`card ${s.rhead}`}>
        <ScoreRing overall={final.overall} averages={averages} headline={headlineOf(final.overall)} />
      </div>

      <div className="card">
        <h3 className={s.cT}>
          <TitleIcon />
          教练总评 <span className={s.cSub}>{gradeOf(final.overall)} · {final.overall.toFixed(1)} / 5</span>
        </h3>
        <div className={s.prose}>
          <Markdown>{final.interview_report}</Markdown>
        </div>
      </div>

      <div className="card">
        <h3 className={s.cT}>
          <GridIcon />
          逐题得分 <span className={s.cSub}>每题三个维度 · 各 1–5 分</span>
        </h3>
        <QTable results={results} overall={final.overall} />
      </div>

      <div className="card">
        <h3 className={s.cT}>
          <TargetIcon />
          薄弱点小结 <span className={s.cSub}>教练划的重点</span>
        </h3>
        {weak.length > 0 ? (
          <ul className={s.wk}>
            {weak.map((w) => (
              <li key={w.topic}>
                <b>{w.topic}</b>
                ：均分 {w.mean.toFixed(1)}
                {w.skipped ? "（含跳过）" : ""}。
                <div className={s.wkLinks}>
                  涉及第{" "}
                  {w.qnums.map((qn, i) => (
                    <span key={qn}>
                      {i > 0 && "、"}
                      <a href={`#q-${qn}`}>{qn}</a>
                    </span>
                  ))}{" "}
                  题，点击可回到对应行
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className={s.wkNone}>本场没有明显短板（各题均分都到了 4 分），下一场可以加大难度或换个岗位域。</p>
        )}
      </div>

      <div className={s.rbtns}>
        <Link className="btn btn-pri btn-big" href={`/?position=${encodeURIComponent(session.position)}`}>
          再来一场
        </Link>
        <Link className="btn btn-ghost btn-big" href="/sessions">
          返回会话列表
        </Link>
      </div>
    </div>
  );
}

export default function ReportShell({ sessionId }: { sessionId: string }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [session, setSession] = useState<SessionState | null>(null);
  const [errMsg, setErrMsg] = useState("");
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    getSession(sessionId).then(
      (sv) => {
        if (!alive) return;
        setSession(sv);
        setPhase("ready");
      },
      (e: unknown) => {
        if (!alive) return;
        setErrMsg(errText(e));
        setPhase("error");
      },
    );
    return () => {
      alive = false;
    };
  }, [sessionId, nonce]);

  if (phase === "error") {
    return (
      <div className="shell">
        <div className={`card ${s.noticeCard}`}>
          <p className={s.err} role="alert">
            {errMsg}
          </p>
          <div className={s.noticeBtns}>
            <button type="button" className="btn btn-pri" onClick={() => setNonce((n) => n + 1)}>
              重试
            </button>
            <Link className="btn btn-ghost" href="/sessions">
              返回会话列表
            </Link>
          </div>
        </div>
      </div>
    );
  }

  if (phase === "loading" || !session) {
    return (
      <div className="shell">
        <div className={`card ${s.noticeCard}`} aria-busy="true" role="status">
          <p>正在整理这场面试的报告…</p>
        </div>
      </div>
    );
  }

  if (session.status !== "finished" || !session.final) {
    return (
      <div className="shell">
        <div className={`card ${s.noticeCard}`}>
          <p>这场面试还没结束——报告要等教练问完全部问题才会生成。</p>
          <div className={s.noticeBtns}>
            <Link className="btn btn-pri" href={`/interview/${sessionId}`}>
              继续面试
            </Link>
            <Link className="btn btn-ghost" href="/sessions">
              返回会话列表
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return <ReportBody session={session} final={session.final} />;
}
