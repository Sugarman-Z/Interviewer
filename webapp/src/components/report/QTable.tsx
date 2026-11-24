"use client";

/**
 * 逐题得分表：表格 + tfoot 维度均分行（比逐题卡片密度高、可横向比对）。
 * 追问行缩进 + 徽标；薄弱格（<3 锈 / <4 琥珀）标色；行 id 供薄弱点卡回链。
 * 375px 降级：td[data-d]::before 出维度标签（thead 隐藏，行变卡片）。
 */
import type { TopicResult } from "@/lib/types";
import { band } from "@/lib/score";
import type { DimAverages } from "./reportLogic";
import { dimAverages, isScored, mean } from "./reportLogic";
import s from "./report.module.css";

const COLS = [
  { key: "technical_depth", label: "技术深度" },
  { key: "correctness", label: "正确性" },
  { key: "communication", label: "表达" },
] as const;

function Score({ v, label }: { v: number | undefined; label: string }) {
  // §10-3：eval_failed 行整键无 scores——占位「未评分」，不伪造 0 分
  if (v === undefined) {
    return (
      <td data-d={label} className={s.num}>
        <span className={s.unscored}>未评分</span>
      </td>
    );
  }
  const b = band(v);
  return (
    <td data-d={label} className={s.num}>
      <span className={`${s.sc} ${s[b]}`}>{v}</span>
    </td>
  );
}

export default function QTable({
  results,
  overall,
}: {
  results: TopicResult[];
  overall: number;
}) {
  const avgs: DimAverages = dimAverages(results);
  return (
    <div className={s.qtWrap}>
      <table className={s.qt}>
        <thead>
          <tr>
            <th scope="col">题号</th>
            <th scope="col">考点</th>
            {COLS.map(({ key, label }) => (
              <th key={key} scope="col">
                {label}
              </th>
            ))}
            <th scope="col">均分</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.qnum} id={`q-${r.qnum}`} className={r.is_followup ? s.fuRow : undefined}>
              <td data-d="题号">
                <span className={s.qn}>
                  {r.is_followup && (
                    <span className={s.fuMark} aria-hidden="true">
                      └
                    </span>
                  )}
                  {r.qnum}
                  {r.is_followup && <span className={s.fuTag}>追问</span>}
                  {r.skipped && <span className={s.skipTag}>已跳过</span>}
                </span>
              </td>
              <td data-d="考点">{r.topic}</td>
              {COLS.map(({ key, label }) => (
                <Score key={key} v={r.scores?.[key]} label={label} />
              ))}
              <td data-d="均分" className={s.num}>
                {isScored(r) ? (
                  <span className={`${s.qavg} ${s[band(mean(r))]}`}>{mean(r).toFixed(1)}</span>
                ) : (
                  <span className={s.unscored}>未评分</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={2}>维度均分</td>
            <td className={s.num}>{avgs.technical_depth.toFixed(1)}</td>
            <td className={s.num}>{avgs.correctness.toFixed(1)}</td>
            <td className={s.num}>{avgs.communication.toFixed(1)}</td>
            <td className={s.num}>
              <b>{overall.toFixed(1)}</b>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
