"use client";

/**
 * 回答输入区：
 * - 可见锁定态：等待/错误期间 textarea 半透明、发送/跳过禁用
 * - Enter 发送 / Shift+Enter 换行（isComposing 守卫：中文 IME 上屏不算发送）
 * - 跳过此题 → 发 {"type":"skip"}
 * - 麦克风禁用占位 + tooltip（语音接缝，不实现录音）
 * - 换题（qnum 变化）自动聚焦输入框（「续面即答」；移动端键盘随交互弹出）
 */
import { useEffect, useRef } from "react";
import s from "./chat.module.css";

interface ComposerProps {
  locked: boolean;
  /** 当前待答题号：变化即聚焦（恢复挂载、每道新题） */
  qnum?: string;
  draft: string;
  onDraftChange: (v: string) => void;
  onSubmit: () => void;
  onSkip: () => void;
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v3" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 2 11 13" />
      <path d="M22 2 15 22l-4-9-9-4z" />
    </svg>
  );
}

function SkipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6 5l7 7-7 7" />
      <path d="M13 5l7 7-7 7" />
    </svg>
  );
}

export default function Composer({ locked, qnum, draft, onDraftChange, onSubmit, onSkip }: ComposerProps) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const canSend = !locked && draft.trim().length > 0;

  // 换题/挂载即聚焦；effect 内只碰 DOM，不 setState
  useEffect(() => {
    if (!locked) taRef.current?.focus();
  }, [qnum, locked]);

  return (
    <footer className={`card ${s.composer} ${locked ? s.lock : ""}`} aria-label="回答输入区">
      <div className={s.cbox}>
        <textarea
          ref={taRef}
          className={s.ta}
          rows={2}
          placeholder="输入你的回答…"
          aria-label="回答输入框"
          value={draft}
          disabled={locked}
          onChange={(e) => onDraftChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.shiftKey) return; // Shift+Enter 走默认换行
            if (e.nativeEvent.isComposing) return; // IME 上屏的 Enter 不算发送
            e.preventDefault();
            if (canSend) onSubmit();
          }}
        />
        <div className={s.crow}>
          <button type="button" className={s.mic} disabled aria-label="语音输入即将支持">
            <MicIcon />
            <span className={s.micTip}>语音输入即将支持</span>
          </button>
          <button type="button" className={s.send} disabled={!canSend} onClick={onSubmit}>
            发送 <SendIcon />
          </button>
        </div>
      </div>
      <div className={s.ctools}>
        <button type="button" className={s.skip} disabled={locked} onClick={onSkip}>
          <SkipIcon />
          跳过此题
        </button>
        <span className={s.chint}>Enter 发送 · Shift + Enter 换行</span>
      </div>
    </footer>
  );
}
