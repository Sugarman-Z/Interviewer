/**
 * 会话列表的纯逻辑（三态/筛选）。
 * 时间与进度文案全部由 SessionSummary 派生，无硬编码场次。
 */
import type { SessionStatus, SessionSummary } from "@/lib/types";

export const FILTERS = [
  { key: "all", label: "全部" },
  { key: "ongoing", label: "进行中" },
  { key: "finished", label: "已完成" },
  { key: "interrupted", label: "已中断" },
] as const;

export type FilterKey = (typeof FILTERS)[number]["key"];

/** 三态中文标签（「已中断」也是可续面的挂起态，动作同为继续面试） */
export const STATUS_LABEL: Record<SessionStatus, string> = {
  ongoing: "进行中",
  finished: "已完成",
  interrupted: "已中断",
};

export function filterSessions(list: SessionSummary[], key: FilterKey): SessionSummary[] {
  return key === "all" ? list : list.filter((sv) => sv.status === key);
}

/**
 * 开始时间的人话格式：今天 14:30 / 昨天 21:14 / 11月14日 / 2025年11月14日。
 * now 可注入供测试；跨午夜用「日界」判断而非 24h 减法（23:50 → 00:10 该算昨天）。
 */
export function formatStartedAt(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const two = (n: number) => String(n).padStart(2, "0");
  const hm = `${two(d.getHours())}:${two(d.getMinutes())}`;
  const dayStart = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((dayStart(now) - dayStart(d)) / 86_400_000);
  if (days <= 0) return `今天 ${hm}`; // 容忍轻微时钟偏差的未来时间
  if (days === 1) return `昨天 ${hm}`;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

/**
 * 行内 meta 文案。挂起态显示「第 N 题」——N 与面试页头部同口径：
 * 优先取 pending_qnum 的主题号（追问 "3.1" 归属第 3 题），字段缺省才回退
 * answered+1；finished 只报题数与时间，分数交给 score 砖。
 */
export function metaLine(sv: SessionSummary, now: Date = new Date()): string {
  const t = formatStartedAt(sv.started_at, now);
  if (sv.status === "finished") {
    return `${sv.progress.answered} 题 · ${t}`;
  }
  const pending = sv.pending_qnum !== undefined && sv.pending_qnum !== "" ? parseInt(sv.pending_qnum, 10) : NaN;
  const cur = Math.min(Number.isFinite(pending) ? pending : sv.progress.answered + 1, sv.progress.total);
  return `第 ${cur} 题 / 共约 ${sv.progress.total} 题 · 开始于 ${t}`;
}
