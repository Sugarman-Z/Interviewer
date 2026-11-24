"use client";

/**
 * 消息流容器：渲染 transcript、新消息自动滚底。
 * 自动滚动纪律：用户上翻（距底 > 80px）即暂停自动滚动并出现「回到底部」；
 * 回到底部或自行滚回底部即恢复。恢复首屏（回放）用 instant，后续追加用 smooth。
 *
 * 结构：.tl 是滚动容器（scroll 事件；键盘焦点锚在 .tlLog 上、方向键滚动
 * 最近滚动祖先），内含 .tlLog（role=log 播报容器，只装已落定的行）与
 * .tlFooter（等待/流式气泡槽）。footer 在播报容器之外（log 隐含
 * aria-live=polite，逐字突变会被逐帧播报）、在同一滚动流之内（打字直接
 * 发生在下一行消息将落位的地方，播完换装零位移）。流式行逐字长高（换行
 * 撑高槽位）由 ResizeObserver 以几何判据跟底（距底 ≤ 阈值才跟——上翻读
 * 历史不被拽回）。
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { TranscriptMessage } from "@/lib/types";
import MessageTurn from "./MessageTurn";
import s from "./chat.module.css";

interface TranscriptProps {
  messages: TranscriptMessage[];
  /** 流内尾部槽位（等待/流式气泡，随内容滚动） */
  footer?: ReactNode;
  /** 播完窗口收拍换装：末行以满文本挂载时跳过 pop 入场（同位同文不再闪） */
  noAnimLast?: boolean;
}

const PAUSE_THRESHOLD_PX = 80;
/** 程序化平滑滚动的动画窗口：期间忽略 scroll 事件，避免「回到底部」闪现/误置暂停 */
const PROGRAMMATIC_WINDOW_MS = 600;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

export default function Transcript({ messages, footer, noAnimLast = false }: TranscriptProps) {
  const ref = useRef<HTMLDivElement>(null);
  const footerRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(false);
  const firstSyncRef = useRef(true);
  const progUntilRef = useRef(0);
  const [showJump, setShowJump] = useState(false);

  const scrollTo = (behavior: "instant" | "smooth") => {
    const el = ref.current;
    if (!el) return;
    const b = behavior === "smooth" && prefersReducedMotion() ? "instant" : behavior;
    // 只有「真会动」的平滑滚动才武装吞噬窗口（评审修正）：已在底部的 no-op
    // 平滑滚动也武装的话，用户在其 600ms 内的上翻事件被吞、pausedRef 无法
    // 置位——ResizeObserver 跟底会把上翻的视口逐字拽回底部（上翻即暂停
    // 纪律失效）。距底 ≤ 阈值的微动画即便误读也置不了暂停
    if (b === "smooth" && el.scrollHeight - el.scrollTop - el.clientHeight > PAUSE_THRESHOLD_PX) {
      progUntilRef.current = Date.now() + PROGRAMMATIC_WINDOW_MS;
    }
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: b });
    } else {
      el.scrollTop = el.scrollHeight; // jsdom 无 scrollTo 实现（测试环境）
    }
  };

  useLayoutEffect(() => {
    if (firstSyncRef.current) {
      scrollTo("instant"); // 恢复回放：paint 前直接置底
      firstSyncRef.current = false;
      return;
    }
    if (!pausedRef.current) scrollTo("smooth");
  }, [messages, footer]);

  // 流式行在滚动流内逐字长高（换行撑高 footer 槽）也要跟底：盯槽位高度变化，
  // **几何判据**（距底 ≤ 阈值才跟，评审修正）——吞噬窗口期用户的上翻事件
  // 可能被吞、pausedRef 未置位，按实际距底判定，滚远了就不拽（上翻读历史
  // 不被打扰）。jsdom 无 ResizeObserver：跳过（滚动几何测试手工造，不受影响）
  const hasFooter = footer != null;
  useEffect(() => {
    const el = footerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      const sc = ref.current;
      if (sc && sc.scrollHeight - sc.scrollTop - sc.clientHeight <= PAUSE_THRESHOLD_PX) {
        scrollTo("instant"); // 连续跟踪不起平滑动画队列
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [hasFooter]);

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    if (Date.now() < progUntilRef.current) return; // 程序化滚动自身触发的事件不算用户上翻
    const el = e.currentTarget;
    const away = el.scrollHeight - el.scrollTop - el.clientHeight > PAUSE_THRESHOLD_PX;
    pausedRef.current = away;
    setShowJump(away);
  };

  const jumpToBottom = () => {
    pausedRef.current = false;
    setShowJump(false);
    scrollTo("smooth");
  };

  return (
    <div className={s.tlWrap}>
      <div className={s.tl} ref={ref} onScroll={handleScroll}>
        {/* 键盘焦点锚（tabIndex）随语义走：落在 log 元素上——读屏播报
            「面试消息流」，方向键滚动最近的滚动祖先 .tl（追查二拆分后不再
            落在无名滚动容器上） */}
        <div
          className={s.tlLog}
          role="log"
          aria-label="面试消息流"
          tabIndex={0} /* 键盘可聚焦：上翻回看历史不需要鼠标（T8 键盘走查） */
        >
          {messages.map((m, i) => (
            <MessageTurn key={i} msg={m} noPop={noAnimLast && i === messages.length - 1} />
          ))}
        </div>
 {/* footer（等待/流式气泡）在 role=log 播报容器**之外**、
            同一滚动流之内（T8 追查二：打字位 = 展示位）：逐字呈现只走视觉
            （US5-1），落定的整题以 MessageTurn 行进入 log 才播报。
            ThinkingBubble 自带 role=status 不受影响 */}
        {footer && (
          <div className={s.tlFooter} ref={footerRef}>
            {footer}
          </div>
        )}
      </div>
      {showJump && (
        <button type="button" className={s.jumpBtn} onClick={jumpToBottom}>
          回到底部 ↓
        </button>
      )}
    </div>
  );
}
