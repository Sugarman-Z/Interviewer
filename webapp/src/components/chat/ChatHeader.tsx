"use client";

/**
 * 聊天页顶栏：岗位 pill + 进度（第 N 题 · 共约 max 题 + 进度条）+ 每题计时器。
 * 计时器：从当前题出现开始计，换题归零；恢复进入的旧会话从 0 计起
 *（页面开着才走字，不含离开页面的时间）。
 */
import { useEffect, useState } from "react";
import s from "./chat.module.css";

interface ChatHeaderProps {
  position: string;
  current: number;
  max: number;
  finished: boolean;
  /** 当前待答题号（换题归零计时器的依据）；finished 时无 */
  qnum?: string;
}

function mmss(totalSec: number): string {
  const m = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

/** 每题计时器：key={qnum} 重挂载即归零（避免 effect 内同步 setState） */
function QuestionTimer() {
  const [sec, setSec] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setSec((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span className={s.timer} aria-label={`本题已用时 ${mmss(sec)}`}>
      {mmss(sec)}
    </span>
  );
}

export default function ChatHeader({ position, current, max, finished, qnum }: ChatHeaderProps) {
  const pct = finished ? 100 : Math.round((current / Math.max(1, max)) * 100);

  return (
    <header className={`card ${s.chatTop}`}>
      <span className={s.postag}>{position}</span>
      <div className={s.prog}>
        <div className={s.progTxt}>
          {finished ? (
            <>
              <b>已收场</b> · 共 {max} 题
            </>
          ) : (
            <>
              <b>第 {current} 题</b> · 共约 {max} 题
            </>
          )}
        </div>
        <div
          className={s.bar}
          role="progressbar"
          aria-label="面试进度"
          aria-valuemin={0}
          aria-valuemax={max}
          aria-valuenow={finished ? max : current}
        >
          <i style={{ width: `${pct}%` }} />
        </div>
      </div>
      {!finished && <QuestionTimer key={qnum} />}
    </header>
  );
}
