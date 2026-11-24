/**
 * 输入向导纯逻辑（无 React 依赖）：岗位/简历文件校验与汇总条派生。
 * 组件与测试共用，规则改动只改这里。
 */
import { POSITION_REQUIRED_MSG } from "@/lib/messages";

/** 单点在 lib/messages（与 Mock 路由 400 同源）；此处转出口供组件用 */
export { POSITION_REQUIRED_MSG };

export const POSITION_CHIPS = [
  "后端开发工程师",
  "前端开发工程师",
  "算法工程师",
  "数据分析师",
  "产品经理",
  "测试开发工程师",
] as const;

export const RESUME_EXT_MSG = "只支持 .txt / .md / .pdf / .docx 文件";
export const RESUME_SIZE_MSG = "文件超过 10 MB 上限，请精简后再上传";

const RESUME_EXTS = [".txt", ".md", ".pdf", ".docx"];
const RESUME_MAX_BYTES = 10 * 1024 * 1024;

/** @returns null = 通过；字符串 = 给用户看的错误文案 */
export function validatePosition(v: string): string | null {
  return v.trim() ? null : POSITION_REQUIRED_MSG;
}

export function validateResumeFile(file: File): string | null {
  const lower = file.name.toLowerCase();
  if (!RESUME_EXTS.some((e) => lower.endsWith(e))) return RESUME_EXT_MSG;
  if (file.size > RESUME_MAX_BYTES) return RESUME_SIZE_MSG;
  return null;
}

export interface SummaryRow {
  label: string;
  value: string;
}

/** 第 3 步汇总条：三项随输入实时派生（T3 验收） */
export function summarize(
  position: string,
  resumeFile: File | null,
  resumeText: string,
  jd: string,
): SummaryRow[] {
  const charCount = (v: string) => v.trim().length;
  return [
    { label: "岗位", value: position.trim() || "未填写" },
    {
      label: "简历",
      value: resumeFile
        ? resumeFile.name
        : charCount(resumeText) > 0
          ? `已粘贴 ${charCount(resumeText)} 字`
          : "未提供",
    },
    {
      label: "JD",
      value: charCount(jd) > 0 ? `已填写 ${charCount(jd)} 字` : "未提供",
    },
  ];
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
