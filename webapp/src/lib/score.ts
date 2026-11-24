/**
 * 分数档位：≥4 绿 / ≥3 琥珀 / <3 锈。
 * 报告页（分数格/均分砖/等级章）与会话列表（综合分砖）共用的跨页规则，
 * 单一定义点在此——各页不得再复制阈值。
 */
export type Band = "g" | "m" | "l";

export function band(v: number): Band {
  if (v >= 4) return "g";
  if (v >= 3) return "m";
  return "l";
}
