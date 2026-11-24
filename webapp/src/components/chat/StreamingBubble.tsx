"use client";

/**
 * 流式气泡（方案 A 打字机）：delta（真机为整块）到达后按 40-80 字/s 自适应
 * 匀速呈现（rate = clamp(len/3, 40, 80)——len ≤ 240 时总时长 ≤3s；>240 字
 * 速率上限侧胜出：80 字/s 线性（400 字 5s），规格的「≤3s」与「≤80 字/s」
 * 在长文本上互斥，按速率上限拍板）；`prefers-reduced-motion` 瞬时全显且
 * 不出光标。
 *
 * 计数与切片按码点（Array.from）——UTF-16 码元切点会劈开代理对（emoji/
 * 扩展区汉字）闪现 U+FFFD。
 *
 * 换装同构：播完窗口里本组件渲染在滚动流尾位，与正式行（MessageTurn
 * 面试官变体）逐像素同构——`head` 传已落定尾行的题面 chip（终局已到，
 * 元数据已知）、`follow` 锁追问形态（缩进+连线+小头像）；正文 p 不另设
 * 字号/行高（度量与 .bub p 一致，全继承 body 14.5px/1.75），收拍换装
 * 零位移零回流。
 *
 * aria：气泡在场即 aria-busy=true（落定 = 气泡整体卸载，「落定后消失」
 * 由卸载实现）；无 aria-live/role=status——组件由 Transcript 渲染在
 * role=log 播报容器之外，逐字突变不进播报，落定的整题经 pending 行进入
 * role=log 才播报。光标只在打字中显示（播完未落定的等待期不演「还在
 * 打字」——断流对账窗口的诚实呈现）。text 只增不减（delta 累积）：
 * 延展即从当前可见处继续播，不清零重头。
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { CoachIcon } from "./MessageTurn";
import s from "./chat.module.css";

const TICK_MS = 50;
const RATE_MIN = 40; // 字/s 下限：短文本也别慢于 40
const RATE_MAX = 80; // 字/s 上限：长文本也别快过 80（>240 字时总时长超过 3s，见头部注）
const DURATION_CAP_S = 3; // len ≤ 240 时的总时长上限：rate ≥ len/3 保证

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

/** 打字机播完一份 length 码点的全文所需毫秒（rate 同 clamp 公式，单源）——
 *  ChatShell 的尾行抑制窗口用它对齐（T8 人工走查修正：真机 delta 与终局
 *  同拍到达，播完窗口由前端补） */
export function typewriterDurationMs(codePoints: number): number {
  const rate = Math.min(RATE_MAX, Math.max(RATE_MIN, codePoints / DURATION_CAP_S));
  return (codePoints / rate) * 1000;
}

export default function StreamingBubble({
  text,
  head,
  follow = false,
}: {
  text: string;
  /** 题面 chip（播完窗口：从已落定尾行取 VariantHead，与正式行同构） */
  head?: ReactNode;
  /** 追问形态：缩进+连线+小头像（与 MessageTurn 的 follow 变体一致） */
  follow?: boolean;
}) {
  const [typed, setTyped] = useState(0); // 已播码点数（reduced-motion 不使用）
  const shownRef = useRef(0);
  const reduced = prefersReducedMotion();

  useEffect(() => {
    if (reduced) {
      shownRef.current = text.length; // 对齐累积器（reduced 下无 tick）
      return;
    }
    // 自适应速率按当前全文长度取（延展后重算）：每 tick 累积小数进度
    const rate = Math.min(RATE_MAX, Math.max(RATE_MIN, text.length / DURATION_CAP_S));
    const perTick = (rate * TICK_MS) / 1000;
    const iv = setInterval(() => {
      const next = Math.min(text.length, shownRef.current + perTick);
      const floor = Math.floor(next);
      shownRef.current = next;
      if (floor !== Math.floor(next - perTick)) {
        setTyped(floor);
      }
      if (next >= text.length) clearInterval(iv);
    }, TICK_MS);
    return () => clearInterval(iv);
  }, [text, reduced]); // reduced 入 deps：播放中系统切换动效偏好即时生效，不卡死

  const visible = reduced ? text.length : typed;
  const cps = Array.from(text); // 码点切片：不劈代理对
  return (
    <div className={`${s.msg} ${s.ai} ${follow ? s.follow : ""}`} data-stream="1">
      <div className={s.ava} aria-hidden="true">
        <CoachIcon size={follow ? 15 : 18} />
      </div>
      <div className={s.bub} aria-busy="true">
        {head}
        <p>
          {cps.slice(0, visible).join("")}
          {!reduced && visible < cps.length && <i className={s.cursor} aria-hidden="true" />}
        </p>
      </div>
    </div>
  );
}
