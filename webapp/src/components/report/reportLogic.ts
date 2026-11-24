/**
 * 报告页纯逻辑：分数聚合、档位判定、薄弱点归并。组件与测试共用。
 * 口径：5 分制，档位色 绿≥4 / 琥珀≥3 / 锈<3。eval_failed 行（scores
 * 整键缺席）不进任何聚合——分子分母同时剔除，禁止 `?? 0` 充数；
 * 展示占位「未评分」在 QTable。
 */
import type { ScoredTopicResult, TopicResult } from "@/lib/types";

export function mean(r: ScoredTopicResult): number {
  const { technical_depth, correctness, communication } = r.scores;
  return (technical_depth + correctness + communication) / 3;
}

/** §10-3 类型收窄：scores 在场且非空（wire 形态整键缺席 / engine 形态
 *  eval_failed 空 dict，两种都算未评分）才可进均分/聚合 */
export const isScored = (r: TopicResult): r is ScoredTopicResult =>
  r.scores !== undefined && Object.keys(r.scores).length > 0;

export interface DimAverages {
  technical_depth: number;
  correctness: number;
  communication: number;
}

const round1 = (v: number) => Math.round(v * 10) / 10;

/** 三维各自的全场均分（tfooter 数据源）；分母 = scores 非空条目数 */
export function dimAverages(results: TopicResult[]): DimAverages {
  const rows = results.filter(isScored);
  const n = Math.max(1, rows.length);
  const sum = (pick: (r: ScoredTopicResult) => number) => rows.reduce((a, r) => a + pick(r), 0);
  return {
    technical_depth: round1(sum((r) => r.scores.technical_depth) / n),
    correctness: round1(sum((r) => r.scores.correctness) / n),
    communication: round1(sum((r) => r.scores.communication) / n),
  };
}

/** 总评等级文案（数据派生，不硬编码场次结论）——档位判定用 @/lib/score 的 band */
export function gradeOf(overall: number): string {
  if (overall >= 4.5) return "优秀";
  if (overall >= 3.5) return "良好";
  if (overall >= 2.5) return "合格";
  return "待提高";
}

export interface WeakPoint {
  topic: string;
  /** 涉及的题号（含追问，如 ["5", "5.1"]），用于回链表格行 */
  qnums: string[];
  mean: number;
  skipped: boolean;
}

/** 薄弱点：均分 <4 的已评分题按考点归并（组均值对整组重算），弱者在前；
 * 未评分行无均分，不参与 */
export function weakPoints(results: TopicResult[]): WeakPoint[] {
  const groups = new Map<string, ScoredTopicResult[]>();
  for (const r of results.filter(isScored)) {
    if (mean(r) >= 4) continue;
    const key = r.topic || `第 ${r.qnum} 题`;
    const arr = groups.get(key);
    if (arr) arr.push(r);
    else groups.set(key, [r]);
  }
  return [...groups.entries()]
    .map(([topic, rows]) => ({
      topic,
      qnums: rows.map((x) => x.qnum),
      mean: round1(rows.reduce((a, x) => a + mean(x), 0) / rows.length),
      skipped: rows.some((x) => x.skipped),
    }))
    .sort((a, b) => a.mean - b.mean);
}
