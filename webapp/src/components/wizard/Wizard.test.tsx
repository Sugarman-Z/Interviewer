// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vitest 未开 globals，RTL 的自动 cleanup 不生效，需显式注册
afterEach(cleanup);

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const createSessionMock = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  // 真实 withTimeoutRetry 进测试（ 重试语义要真跑），只桩网络函数
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createSession: (p: unknown, signal?: AbortSignal) => createSessionMock(p, signal),
  };
});

import { ApiError } from "@/lib/api";
import Wizard from "./Wizard";

/** 填岗位并连点两次下一步，到第 3 步 */
function gotoStep3(position = "后端开发工程师") {
  fireEvent.change(screen.getByLabelText("目标岗位 *"), { target: { value: position } });
  fireEvent.click(screen.getByRole("button", { name: "下一步" }));
  fireEvent.click(screen.getByRole("button", { name: "下一步" }));
}

beforeEach(() => {
  pushMock.mockReset();
  createSessionMock.mockReset();
});

describe("第 1 步 · 岗位必填", () => {
  it("空岗位点下一步不进第二步，红字提示并保持输入可见", () => {
    render(<Wizard />);
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByText("岗位还没填——教练需要它来为你出题")).toBeTruthy();
    expect(screen.queryByText("上传文件")).toBeNull();
  });

  it("点快捷 chip 直接填入岗位，再点下一步进入第 2 步", () => {
    render(<Wizard />);
    fireEvent.click(screen.getByRole("button", { name: "算法工程师" }));
    expect((screen.getByLabelText("目标岗位 *") as HTMLInputElement).value).toBe("算法工程师");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByText("上传文件")).toBeTruthy();
  });

  it("输入框按 Enter 等价于下一步", () => {
    render(<Wizard />);
    fireEvent.change(screen.getByLabelText("目标岗位 *"), { target: { value: "后端" } });
    fireEvent.keyDown(screen.getByLabelText("目标岗位 *"), { key: "Enter" });
    expect(screen.getByText("上传文件")).toBeTruthy();
  });

  it("中文 IME 组合态的 Enter（上屏候选词）不触发下一步", () => {
    render(<Wizard />);
    fireEvent.change(screen.getByLabelText("目标岗位 *"), { target: { value: "后" } });
    fireEvent.keyDown(screen.getByLabelText("目标岗位 *"), { key: "Enter", isComposing: true });
    expect(screen.queryByText("上传文件")).toBeNull();
    fireEvent.keyDown(screen.getByLabelText("目标岗位 *"), { key: "Enter" });
    expect(screen.getByText("上传文件")).toBeTruthy();
  });
});

describe("第 2 步 · 简历双轨", () => {
  function gotoStep2() {
    fireEvent.change(screen.getByLabelText("目标岗位 *"), { target: { value: "后端" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
  }

  function fileInput() {
    return document.querySelector('input[type="file"]') as HTMLInputElement;
  }

  it("选择合法文件后展示就绪行（文件名+大小），可用 × 移除", () => {
    render(<Wizard />);
    gotoStep2();
    fireEvent.change(fileInput(), {
      target: { files: [new File(["resume"], "简历.txt", { type: "text/plain" })] },
    });
    expect(screen.getByText("简历.txt")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "移除简历文件" }));
    expect(screen.queryByText("简历.txt")).toBeNull();
    expect(screen.getByText("点击或拖拽简历到这里")).toBeTruthy();
  });

  it("非法扩展名文件被拒绝并红字提示", () => {
    render(<Wizard />);
    gotoStep2();
    fireEvent.change(fileInput(), {
      target: { files: [new File(["x"], "a.exe")] },
    });
    expect(screen.getByText(/只支持/)).toBeTruthy();
    expect(screen.queryByText("a.exe")).toBeNull();
  });

  it("上传与粘贴互斥：选文件清空粘贴，粘贴清掉文件", () => {
    render(<Wizard />);
    gotoStep2();
    const paste = screen.getByPlaceholderText(/把简历文本粘贴到这里/) as HTMLTextAreaElement;

    fireEvent.change(fileInput(), {
      target: { files: [new File(["r"], "a.txt", { type: "text/plain" })] },
    });
    expect(screen.getByText("a.txt")).toBeTruthy();

    fireEvent.change(paste, { target: { value: "十年后端经验" } });
    expect(screen.queryByText("a.txt")).toBeNull(); // 粘贴即弃文件
    expect(paste.value).toBe("十年后端经验");
    expect(screen.getByText(/已改用粘贴文本/)).toBeTruthy(); // 切换有告知，非静默销毁

    fireEvent.change(fileInput(), {
      target: { files: [new File(["r"], "b.pdf", { type: "application/pdf" })] },
    });
    expect(screen.getByText("b.pdf")).toBeTruthy();
    expect(paste.value).toBe(""); // 选文件清空粘贴
    expect(screen.getByText(/已改用上传文件/)).toBeTruthy();
  });

  it("文件校验失败后改走粘贴轨道，陈旧红字被清除", () => {
    render(<Wizard />);
    gotoStep2();
    fireEvent.change(fileInput(), { target: { files: [new File(["x"], "a.exe")] } });
    expect(screen.getByText(/只支持/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("粘贴简历文本"), { target: { value: "abc" } });
    expect(screen.queryByText(/只支持/)).toBeNull();
  });

  it("两者都不提供时出现「仅依据岗位与自我介绍」提示，粘贴后消失", () => {
    render(<Wizard />);
    gotoStep2();
    expect(screen.getByText(/仅依据岗位与你的自我介绍/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText(/把简历文本粘贴到这里/), {
      target: { value: "abc" },
    });
    expect(screen.queryByText(/仅依据岗位与你的自我介绍/)).toBeNull();
  });
});

describe("第 3 步 · 汇总条与提交", () => {
  it("汇总条随输入实时变化", () => {
    render(<Wizard />);
    gotoStep3("数据分析师");
    // 岗位行 + 简历/JD 两行未提供
    expect(screen.getByText("数据分析师")).toBeTruthy();
    expect(screen.getAllByText("未提供")).toHaveLength(2);

    fireEvent.change(screen.getByPlaceholderText(/把招聘 JD 粘贴到这里/), {
      target: { value: "负责高并发" },
    });
    expect(screen.getByText("已填写 5 字")).toBeTruthy();
  });

  it("粘贴的简历在汇总条以字数呈现", () => {
    render(<Wizard />);
    fireEvent.change(screen.getByLabelText("目标岗位 *"), { target: { value: "后端" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    fireEvent.change(screen.getByPlaceholderText(/把简历文本粘贴到这里/), {
      target: { value: "abcde" },
    });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByText("已粘贴 5 字")).toBeTruthy();
  });

  it("提交成功：携带载荷调用 createSession 并跳转 /interview/[id]", async () => {
    createSessionMock.mockResolvedValue({
      session_id: "s-xyz",
      max_questions: 10,
      question: { variant: "opening", text: "请自我介绍" },
    });
    render(<Wizard />);
    gotoStep3("后端");
    fireEvent.change(screen.getByPlaceholderText(/把招聘 JD 粘贴到这里/), {
      target: { value: "高并发" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始面试" }));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/interview/s-xyz"));
    expect(createSessionMock).toHaveBeenCalledWith(
      {
        position: "后端",
        jd: "高并发",
        resumeText: undefined,
        resumeFile: undefined,
      },
      expect.anything(),
    );
  });

  it("§10-6 最小韧性：创建失败自动重试一次，成功即照常跳转", async () => {
    createSessionMock
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValue({
        session_id: "s-retry",
        max_questions: 10,
        question: { variant: "opening", text: "请自我介绍" },
      });
    render(<Wizard />);
    gotoStep3("后端");
    fireEvent.click(screen.getByRole("button", { name: "开始面试" }));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/interview/s-retry"));
    expect(createSessionMock).toHaveBeenCalledTimes(2);
  });

  it("提交失败：错误原位显示，表单内容保留，按钮恢复可用", async () => {
    createSessionMock.mockRejectedValue(new ApiError(500, "服务开小差了"));
    render(<Wizard />);
    gotoStep3("前端");
    fireEvent.click(screen.getByRole("button", { name: "开始面试" }));

    expect(await screen.findByText("服务开小差了")).toBeTruthy();
    const btn = screen.getByRole("button", { name: "开始面试" }) as HTMLButtonElement;
    expect(btn.disabled).toBeFalsy();

    // 数据没被清空：退回第 1 步岗位还在
    fireEvent.click(screen.getByRole("button", { name: "上一步" }));
    fireEvent.click(screen.getByRole("button", { name: "上一步" }));
    expect((screen.getByLabelText("目标岗位 *") as HTMLInputElement).value).toBe("前端");
  });

  it("非 ApiError 的意外错误给通用文案", async () => {
    createSessionMock.mockRejectedValue(new Error("network down"));
    render(<Wizard />);
    gotoStep3();
    fireEvent.click(screen.getByRole("button", { name: "开始面试" }));
    expect(await screen.findByText("创建会话失败，请检查网络后重试")).toBeTruthy();
  });

  it("提交进行中：按钮转入加载态，指示器回退与上一步均锁定，成功后照常跳转", async () => {
    let resolveCreate!: (v: unknown) => void;
    createSessionMock.mockReturnValue(
      new Promise((r) => {
        resolveCreate = r;
      }),
    );
    render(<Wizard />);
    gotoStep3();
    fireEvent.click(screen.getByRole("button", { name: "开始面试" }));

    const busy = screen.getByRole("button", { name: "正在开始…" }) as HTMLButtonElement;
    expect(busy.disabled).toBeTruthy();
    expect((screen.getByRole("button", { name: "上一步" }) as HTMLButtonElement).disabled).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "回到第 1 步：选择岗位" }) as HTMLButtonElement).disabled,
    ).toBeTruthy();

    resolveCreate({ session_id: "s-lock", max_questions: 10, question: { variant: "opening", text: "请自我介绍" } });
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/interview/s-lock"));
  });
});

describe("步骤指示器回退", () => {
  it("已完成步骤的圆点可点回退，当前/未来步骤不渲染为按钮", () => {
    render(<Wizard />);
    // 第 1 步：没有任何可回退的圆点
    expect(screen.queryByRole("button", { name: /回到第/ })).toBeNull();
    gotoStep3();
    fireEvent.click(screen.getByRole("button", { name: "回到第 1 步：选择岗位" }));
    expect(screen.getByLabelText("目标岗位 *")).toBeTruthy();
    expect(screen.queryByLabelText(/职位描述/)).toBeNull();

    // 第 1 步再次确认：第 2/3 步圆点不是按钮（当前步=1）
    expect(screen.queryByRole("button", { name: /回到第/ })).toBeNull();
  });

  it("换步后焦点移入新面板（不落在 body 上）", () => {
    render(<Wizard />);
    fireEvent.change(screen.getByLabelText("目标岗位 *"), { target: { value: "后端" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    const panel = document.querySelector('[role="group"]') as HTMLElement;
    expect(document.activeElement).toBe(panel);
  });
});
