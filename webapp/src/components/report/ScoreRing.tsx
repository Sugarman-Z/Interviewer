"use client";

/**
 * 综合分圆环 + 三维均值条（5 分制 X.X / 5，不用百分制）。
 * 圆环进度 = overall / 5；等级文案与色带全部数据派生。
 */
import { band } from "@/lib/score";
import type { DimAverages } from "./reportLogic";
import { gradeOf } from "./reportLogic";
import s from "./report.module.css";

const R = 53;
const CIRC = 2 * Math.PI * R;

const DIM_LABELS: Array<{ key: keyof DimAverages; label: string }> = [
  { key: "technical_depth", label: "技术深度" },
  { key: "correctness", label: "正确性" },
  { key: "communication", label: "表达" },
];

export default function ScoreRing({
  overall,
  averages,
  headline,
}: {
  overall: number;
  averages: DimAverages;
  /** 按等级派生的一句话（ReportShell 生成） */
  headline: string;
}) {
  const frac = Math.max(0, Math.min(1, overall / 5));
  const grade = gradeOf(overall);

  return (
    <div className={s.rhead}>
      <div
        className={s.ring}
        role="img"
        aria-label={`综合评分 ${overall.toFixed(1)} / 5，${grade}`}
      >
        <svg viewBox="0 0 122 122" width="122" height="122">
          <circle cx="61" cy="61" r={R} fill="none" stroke="var(--line-2)" strokeWidth="10" />
          <circle
            cx="61"
            cy="61"
            r={R}
            fill="none"
            stroke="var(--orange)"
            strokeWidth="10"
            strokeLinecap="round"
            strokeDasharray={`${(CIRC * frac).toFixed(1)} ${CIRC.toFixed(1)}`}
          />
        </svg>
        <div className={s.ringC}>
          <b>{overall.toFixed(1)}</b>
          <span>/ 5 综合分</span>
        </div>
      </div>
      <div className={s.rinfo}>
        <span className={`${s.grade} ${s["b" + band(overall)]}`}>{grade}</span>
        <h2>{headline}</h2>
        <div className={s.rdims}>
          {DIM_LABELS.map(({ key, label }) => (
            <div className={s.rdim} key={key}>
              <span>{label}</span>
              <div className={s.bar}>
                <i style={{ width: `${(averages[key] / 5) * 100}%` }} />
              </div>
              <b>{averages[key].toFixed(1)}</b>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
