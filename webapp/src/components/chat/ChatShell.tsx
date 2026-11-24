"use client";

/**
 * 面试页客户端壳：布局 + 状态机接线（useInterview）。
 * restore/restore-error → 恢复卡片；await-input/thinking/send-error → 消息流 +
 * Composer（等待期可见锁定）；finished → 引导报告页（恢复进入不自动跳，收场自动跳）。
 *
 * 落定播完窗口：真机形态下 delta（args 原子落地整块）与终局 question 事件
 * 同一拍到达且被 React 批成一次提交——「落定即替换」会让打字机拿到 ~0ms
 * 播放窗口、逐字永远不可见。useInterview 在终局落定时给出 `playOutText`
 *（事件流闭包捕获，不依赖中间态提交）：窗口内抑制 transcript 尾行（防
 * 双文本源）、流式气泡继续播完；输入解锁即时不受影响。流式行渲染在滚动
 * 流尾位（Transcript 的 footer 槽）与正式行同构播完——含题面 chip/追问
 * 缩进（打字位 = 展示位），收拍换装零位移，换装行经 noAnimLast 跳过
 * pop 入场。
 */
import Link from "next/link";
import { useInterview } from "@/lib/useInterview";
import type { SessionState, TranscriptMessage } from "@/lib/types";
import ChatHeader from "./ChatHeader";
import Transcript from "./Transcript";
import ThinkingBubble from "./ThinkingBubble";
import StreamingBubble from "./StreamingBubble";
import Composer from "./Composer";
import { VariantHead } from "./MessageTurn";
import s from "./chat.module.css";

/** 顶栏「第 N 题」：优先取当前待答题号（追问 3.1 归属第 3 题） */
function currentNo(session: SessionState): number {
  const qn = session.pending_question?.qnum;
  if (qn) return Number.parseInt(qn, 10);
  return session.progress.answered + 1;
}

export default function ChatShell({ sessionId }: { sessionId: string }) {
  const iv = useInterview(sessionId);
  const { stage, session } = iv;
  const finished = session?.status === "finished";
  // send-error 也锁定：错误态下「重试」是唯一出口（ 保留输入=展示，不必可编辑），
  // 避免出现可点但无响应的发送/跳过
  const locked =
    stage.kind === "restore" || stage.kind === "thinking" || stage.kind === "send-error" || stage.kind === "finished";

    // 播完收拍：hook 的 playOutJustEnded 标记「刚从窗口收拍」——
  // 换装的正式尾行以满文本挂载，跳过 pop 入场（同位同文再播一次淡入会闪）；
  // 只对面试官尾行生效（final 轮无尾行换装）
  const lastMsg = iv.messages.at(-1);
  const noAnimLast = iv.playOutJustEnded && lastMsg?.role === "interviewer";

  // 播完窗口内：末行（刚落定的题面）抑制，气泡播完由 useInterview 撤下后放行
  const shown: TranscriptMessage[] =
    iv.playOutText && iv.messages.length > 0 ? iv.messages.slice(0, -1) : iv.messages;
    // 窗口内流式行与正式行同构：终局已到，题面 chip/追问缩进
  // 从已落定尾行取——换装瞬间 DOM 同形，零位移零回流
  const playTail = iv.playOutText ? iv.messages.at(-1) : undefined;
  const playHead = playTail?.role === "interviewer" ? playTail : undefined;

  return (
    <div className={s.chatpage}>
      <div className={`shell ${s.col}`}>
        {session && (
          <>
            {/* sr-only h1：面试页唯一的页面级标题（读屏/文档结构，T8） */}
            <h1 className={s.srOnly}>{session.position} · 模拟面试</h1>
            <ChatHeader
              position={session.position}
              current={finished ? session.progress.total : currentNo(session)}
              max={session.max_questions}
              finished={finished}
              qnum={session.pending_question?.qnum}
            />
          </>
        )}

        {stage.kind === "restore" && (
          <div className={s.tl} aria-busy="true">
            <RestoreBubble />
          </div>
        )}

        {stage.kind === "restore-error" && (
          <div className={`card ${s.errCard}`}>
            <p role="alert">{stage.message}</p>
            <div className={s.errBtns}>
              <button type="button" className="btn btn-pri" onClick={iv.retryRestore}>
                重试
              </button>
              <Link className="btn btn-ghost" href="/sessions">
                返回会话列表
              </Link>
            </div>
          </div>
        )}

        {session && stage.kind !== "restore-error" && (
          <>
            <Transcript
              messages={shown}
              noAnimLast={noAnimLast}
              footer={
                stage.kind === "thinking" ? (
                // streamText 非空 → 流式气泡（方案 A 打字机）；
                  // 为空 → ThinkingBubble——phase 已知时文案由 phase 驱动且
                  // 定时自切禁用。互斥渲染防双气泡
                  stage.streamText ? (
                    <StreamingBubble text={stage.streamText} />
                  ) : (
                    <ThinkingBubble phase={stage.phase} />
                  )
                ) : iv.playOutText ? (
                  // 播完窗口：终局已落定（输入已解锁），气泡在滚动流尾位以
                  // 正式行同构形态播完（含题面 chip），播完由 hook 撤下换装
                  <StreamingBubble
                    text={iv.playOutText}
                    head={playHead ? <VariantHead msg={playHead} /> : undefined}
                    follow={playHead?.variant === "followup"}
                  />
                ) : undefined
              }
            />

            {stage.kind === "send-error" && (
              <div className={s.errBanner} role="alert">
                <div className={s.errBannerMain}>{stage.message}</div>
                <div className={s.errBannerSub}>
                  {stage.attempted.type === "answer"
                    ? "你的回答已保留在输入框。重试前会先和教练核对进度，不会把同一条回答记两次。"
                    : "重试前会先和教练核对进度，不会重复记分。"}
                </div>
                <button type="button" className="btn btn-pri btn-sm" onClick={iv.retry}>
                  重试
                </button>
              </div>
            )}

            {finished ? (
              <footer className={`card ${s.doneBar}`}>
                <p>本场面试已结束——教练把终场报告准备好了，去看看你的表现。</p>
                <div className={s.doneBtns}>
                  <Link className="btn btn-pri" href={`/report/${sessionId}`}>
                    查看终场报告
                  </Link>
                  <Link className="btn btn-ghost" href="/sessions">
                    返回会话列表
                  </Link>
                </div>
              </footer>
            ) : (
              <Composer
                locked={locked}
                qnum={session.pending_question?.qnum}
                draft={iv.draft}
                onDraftChange={iv.setDraft}
                onSubmit={iv.submit}
                onSkip={iv.skip}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function RestoreBubble() {
  return (
    <div className={`${s.msg} ${s.ai} ${s.loading}`}>
      <div className={s.ava} aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21c0-4 3.6-6.5 8-6.5s8 2.5 8 6.5" />
        </svg>
      </div>
      <div className={s.bub} role="status">
        <div className={s.ldots}>
          <i />
          <i />
          <i />
        </div>
        <div className={s.ltext}>正在恢复这场面试…</div>
      </div>
    </div>
  );
}
