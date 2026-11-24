"use client";

/**
 * 会话列表：挂载 GET /api/sessions 渲染。
 * 三态行（进行中呼吸点/已完成/已中断）+ 状态筛选；挂起态（进行中/已中断）→ 继续面试，
 * 已完成 → 查看报告；全空 → 「还没有面试，来一场」。排序信任服务端（新→旧）。
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, listSessions, withTimeoutRetry } from "@/lib/api";
import { band } from "@/lib/score";
import type { SessionSummary } from "@/lib/types";
import { FILTERS, STATUS_LABEL, filterSessions, metaLine, type FilterKey } from "./sessionsLogic";
import s from "./sessions.module.css";

type Phase = "loading" | "error" | "ready";

/** §10-6 最小韧性：单次超时 + 失败立即重试一次 */
const LIST_TIMEOUT_MS = 30_000;

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return "会话列表加载失败，请重试";
}

function CalIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="5" width="16" height="16" rx="2" />
      <path d="M8 3v4M16 3v4M4 10h16" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function SessionRow({ sv }: { sv: SessionSummary }) {
  const finished = sv.status === "finished";
  return (
    <article
      className={`card ${s.scard} ${finished ? s.done : sv.status === "interrupted" ? s.int : ""}`}
    >
      <div className={s.sico} aria-hidden="true">
        <CalIcon />
      </div>
      <div className={s.smain}>
        <div className={s.st}>
          <b>{sv.position}</b>
          <span className={`${s.stag} ${s[sv.status]}`}>
            {sv.status === "ongoing" && <i className={s.dot} aria-hidden="true" />}
            {STATUS_LABEL[sv.status]}
          </span>
        </div>
        <p className={s.sm}>{metaLine(sv)}</p>
      </div>
      <div className={s.sside}>
        {finished && sv.overall !== undefined && (
          <div
            role="img"
            className={`${s.sscore} ${s[band(sv.overall)]}`}
            aria-label={`综合评分 ${sv.overall.toFixed(1)} / 5`}
          >
            {sv.overall.toFixed(1)}
          </div>
        )}
        <Link
          className={`btn btn-sm ${finished ? "btn-ghost" : "btn-pri"}`}
          href={finished ? `/report/${sv.session_id}` : `/interview/${sv.session_id}`}
        >
          {finished ? "查看报告" : "继续面试"}
        </Link>
      </div>
    </article>
  );
}

export default function SessionList() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [list, setList] = useState<SessionSummary[] | null>(null);
  const [errMsg, setErrMsg] = useState("");
  const [nonce, setNonce] = useState(0);
  const [filter, setFilter] = useState<FilterKey>("all");

  useEffect(() => {
    let alive = true;
    withTimeoutRetry((sig) => listSessions(sig), LIST_TIMEOUT_MS).then(
      (rows) => {
        if (!alive) return;
        setList(rows);
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
  }, [nonce]);

  const head = (
    <div className={`page-head ${s.headRow}`}>
      <div>
        <h1>历史会话</h1>
        <p>{phase === "ready" && list ? `共 ${list.length} 场 · 未完成的随时接着练` : "未完成的随时接着练"}</p>
      </div>
      <Link className="btn btn-pri" href="/">
        <PlusIcon />
        开始新面试
      </Link>
    </div>
  );

  if (phase === "error") {
    return (
      <div className={`shell ${s.scol}`}>
        {head}
        <div className={`card ${s.noticeCard}`}>
          <p className={s.err} role="alert">
            {errMsg}
          </p>
          <div className={s.noticeBtns}>
            <button type="button" className="btn btn-pri" onClick={() => setNonce((n) => n + 1)}>
              重试
            </button>
            <Link className="btn btn-ghost" href="/">
              去开一场
            </Link>
          </div>
        </div>
      </div>
    );
  }

  if (phase === "loading" || !list) {
    return (
      <div className={`shell ${s.scol}`}>
        {head}
        <div className={`card ${s.noticeCard}`} aria-busy="true" role="status">
          <p>正在取回你的面试记录…</p>
        </div>
      </div>
    );
  }

  const shown = filterSessions(list, filter);

  return (
    <div className={`shell ${s.scol}`}>
      {head}
      {list.length > 0 && (
        <div className={s.filters} role="group" aria-label="按状态筛选">
          {FILTERS.map(({ key, label }) => (
            <button
              key={key}
              type="button"
              className={`${s.chip} ${filter === key ? s.on : ""}`}
              aria-pressed={filter === key}
              onClick={() => setFilter(key)}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {list.length === 0 ? (
        <div className={`card ${s.empty}`}>
          <p>还没有面试，来一场——岗位填好，教练马上出题。</p>
          <Link className="btn btn-pri btn-big" href="/">
            开始新面试
          </Link>
        </div>
      ) : shown.length === 0 ? (
        <p className={s.none}>没有{filter !== "all" ? STATUS_LABEL[filter] : ""}的会话</p>
      ) : (
        <div className={s.slist}>
          {shown.map((sv) => (
            <SessionRow key={sv.session_id} sv={sv} />
          ))}
        </div>
      )}
    </div>
  );
}
