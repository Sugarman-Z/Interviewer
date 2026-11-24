import { describe, expect, it } from "vitest";
import {
  formatBytes,
  summarize,
  validatePosition,
  validateResumeFile,
} from "./wizardLogic";

describe("validatePosition", () => {
  it("空与纯空白不通过，文案与后端 400 一致", () => {
    expect(validatePosition("")).toBe("岗位还没填——教练需要它来为你出题");
    expect(validatePosition("   ")).toBe("岗位还没填——教练需要它来为你出题");
  });

  it("非空（含前后空白）通过", () => {
    expect(validatePosition(" 后端开发工程师 ")).toBeNull();
  });
});

describe("validateResumeFile", () => {
  const f = (name: string) => new File(["x"], name, { type: "text/plain" });

  it("白名单扩展名通过，大小写不敏感", () => {
    for (const name of ["a.txt", "b.MD", "简历.PDF", "d.docx"]) {
      expect(validateResumeFile(f(name))).toBeNull();
    }
  });

  it("非白名单扩展名拒绝", () => {
    expect(validateResumeFile(f("a.exe"))).toContain("只支持");
    expect(validateResumeFile(f("无扩展名"))).toContain("只支持");
  });

  it("超过 10 MB 拒绝，恰好 10 MB 通过", () => {
    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], "a.txt");
    expect(validateResumeFile(big)).toContain("10 MB");
    const edge = new File([new Uint8Array(10 * 1024 * 1024)], "a.txt");
    expect(validateResumeFile(edge)).toBeNull();
  });
});

describe("summarize · 汇总条三态", () => {
  it("全空：未填写 / 未提供 / 未提供", () => {
    expect(summarize("", null, "", "")).toEqual([
      { label: "岗位", value: "未填写" },
      { label: "简历", value: "未提供" },
      { label: "JD", value: "未提供" },
    ]);
  });

  it("文件优先展示文件名（即使粘贴框也有残留，由组件层保证互斥）", () => {
    const rows = summarize("后端", new File(["x"], "简历.pdf"), "", "");
    expect(rows[0].value).toBe("后端");
    expect(rows[1].value).toBe("简历.pdf");
  });

  it("粘贴文本按去空白后字数统计", () => {
    expect(summarize("后端", null, "  abc def  ", "负责高并发").map((r) => r.value)).toEqual([
      "后端",
      "已粘贴 7 字",
      "已填写 5 字",
    ]);
  });
});

describe("formatBytes", () => {
  it("B / KB / MB 三档", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(10 * 1024 * 1024)).toBe("10.0 MB");
  });
});
