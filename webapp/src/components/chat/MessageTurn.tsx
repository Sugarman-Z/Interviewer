"use client";

/**
 * 单条消息渲染：按 role/variant 分派四种面试官变体（opening/question/followup/closing）、
 * 候选人回答/跳过、系统条。追问与主题题的视觉区分（缩进+连线+徽标+同考点 tag）在这里落实。
 *
 * 候选人行带「我」字圆头像：answer/skip 两形态一致，row-reverse 使头像
 * 呈现在气泡右侧。
 *
 * noPop：播完窗口收拍换装时，正式行与流式行同位同文——挂载跳过 pop 入场
 * 动画（同位同文再播一次淡入会闪）。只在挂载拍取值锁进 state：之后 prop
 * 翻回 false 摘掉 class 也不会把动画补播出来。
 */
import { useState } from "react";
import type { TranscriptMessage } from "@/lib/types";
import s from "./chat.module.css";

export function CoachIcon({ size = 18 }: { size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21c0-4 3.6-6.5 8-6.5s8 2.5 8 6.5" />
    </svg>
  );
}

function BoltIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="10"
      height="10"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M13 2 4 14h6l-1 8 9-12h-6z" />
    </svg>
  );
}

/** 面试官消息的头部 chip：开场/收场用静态标签，主题题带 Q 编号 + 考点，追问带徽标 + 同考点。
 *  导出供 StreamingBubble 换装同构使用（播完窗口的流式行与正式行同款头部） */
export function VariantHead({ msg }: { msg: TranscriptMessage }) {
  if (msg.variant === "opening") {
    return (
      <div className={s.qhead}>
        <span className={`${s.qtag} ${s.alt}`}>开场 · 自我介绍</span>
      </div>
    );
  }
  if (msg.variant === "closing") {
    return (
      <div className={s.qhead}>
        <span className={`${s.qtag} ${s.alt}`}>收场</span>
      </div>
    );
  }
  if (msg.variant === "followup") {
    return (
      <div className={s.qhead}>
        <span className={s.fbadge}>
          <BoltIcon />
          追问
        </span>
        <span className={`${s.qtag} ${s.alt}`}>同考点 · {msg.topic}</span>
      </div>
    );
  }
  // question
  return (
    <div className={s.qhead}>
      <span className={s.qtag}>
        Q{msg.qnum} · {msg.topic}
      </span>
    </div>
  );
}

export default function MessageTurn({ msg, noPop = false }: { msg: TranscriptMessage; noPop?: boolean }) {
  const [silentEntry] = useState(noPop);
  const rowCls = silentEntry ? `${s.msg} ${s.noPop}` : s.msg;

  if (msg.role === "system") {
    return <div className={s.sysline}>{msg.text}</div>;
  }

  if (msg.role === "candidate") {
    if (msg.variant === "skip") {
      return (
        <div className={`${rowCls} ${s.me}`} data-nopop={silentEntry ? "1" : undefined}>
          <div className={`${s.ava} ${s.avaMe}`} aria-hidden="true">
            我
          </div>
          <div className={`${s.bub} ${s.skipBub}`}>
            <p>已跳过此题</p>
          </div>
        </div>
      );
    }
    return (
      <div className={`${rowCls} ${s.me}`} data-nopop={silentEntry ? "1" : undefined}>
        <div className={`${s.ava} ${s.avaMe}`} aria-hidden="true">
          我
        </div>
        <div className={s.bub}>
          <p>{msg.text}</p>
        </div>
      </div>
    );
  }

  // interviewer
  const isFollowup = msg.variant === "followup";
  return (
    <div className={`${rowCls} ${s.ai} ${isFollowup ? s.follow : ""}`} data-nopop={silentEntry ? "1" : undefined}>
      <div className={s.ava}>
        <CoachIcon size={isFollowup ? 15 : 18} />
      </div>
      <div className={s.bub}>
        <VariantHead msg={msg} />
        <p>{msg.text}</p>
      </div>
    </div>
  );
}
