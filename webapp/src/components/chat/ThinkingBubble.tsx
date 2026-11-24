"use client";

/**
 * 等待态气泡（四级文案的前三级）：呼吸点 + 分阶段文案。
 * 两种形态：
 * - 带 `phase`（流式路径，stage.phase 已知）：文案由 phase 驱动，定时自切
 *   禁用——服务端 stage 事件才是事实源，本地定时器会在静默段瞎切（评估
 *   45s+、报告 40-120s 都远超 8s/30s 档）；
 * - 无 `phase`（legacy POST 回退、恢复/对账路径）：现行 8s/30s 定时自切
 *   原样保留。第 4 级（超时/失败）在 useInterview 里转 send-error，不归这里。
 */
import { useEffect, useState } from "react";
import type { StreamPhase } from "@/lib/types";
import s from "./chat.module.css";

export const THINKING_COPY = [
  "正在评估你的回答…",
  "正在准备下一个问题…",
  "面试官思考较久，请稍候——你的回答已保存，可放心等待",
] as const;

/** 流式态词汇表：与服务端 stage 事件一一对应。issuing 措辞与
 *  定时档的「准备下一个问题」刻意不同——流式路径里 issuing 后面紧跟
 *  逐字出题，「组织」是 SPEC-streaming §4 验收行的原文 */
export const PHASE_COPY: Record<StreamPhase, string> = {
  evaluating: "正在评估你的回答…",
  issuing: "正在组织下一个问题…",
  reporting: "正在生成终场报告…",
};

const PHASE2_MS = 8_000;
const PHASE3_MS = 30_000;

export default function ThinkingBubble({ phase }: { phase?: StreamPhase }) {
  const [tick, setTick] = useState<1 | 2 | 3>(1);

  useEffect(() => {
    if (phase) return; // phase 已知：定时自切禁用（US2-2）
    const t2 = setTimeout(() => setTick(2), PHASE2_MS);
    const t3 = setTimeout(() => setTick(3), PHASE3_MS);
    return () => {
      clearTimeout(t2);
      clearTimeout(t3);
    };
  }, [phase]);

  const copy = phase ? PHASE_COPY[phase] : THINKING_COPY[tick - 1];

  return (
    <div className={`${s.msg} ${s.ai} ${s.loading}`}>
      <div className={s.ava} aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21c0-4 3.6-6.5 8-6.5s8 2.5 8 6.5" />
        </svg>
      </div>
      <div className={s.bub} role="status" aria-live="polite">
        <div className={s.ldots}>
          <i />
          <i />
          <i />
        </div>
        <div className={s.ltext}>{copy}</div>
      </div>
    </div>
  );
}
