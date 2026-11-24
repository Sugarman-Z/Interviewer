// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import Composer from "./Composer";

afterEach(cleanup);

/** 受控包装：真实走 draft 状态往返 */
function StatefulComposer({ locked = false, onSubmit, onSkip }: { locked?: boolean; onSubmit: () => void; onSkip: () => void }) {
  const [draft, setDraft] = useState("");
  return (
    <Composer locked={locked} draft={draft} onDraftChange={setDraft} onSubmit={onSubmit} onSkip={onSkip} />
  );
}

describe("Composer · 输入与发送", () => {
  it("空草稿发送禁用；输入后 Enter 提交", () => {
    const onSubmit = vi.fn();
    render(<StatefulComposer onSubmit={onSubmit} onSkip={() => {}} />);
    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    const send = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(send.disabled).toBeTruthy();
    fireEvent.change(ta, { target: { value: "我的回答" } });
    expect(send.disabled).toBeFalsy();
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("Shift+Enter 不提交（换行走默认行为）", () => {
    const onSubmit = vi.fn();
    render(<StatefulComposer onSubmit={onSubmit} onSkip={() => {}} />);
    fireEvent.change(screen.getByLabelText("回答输入框"), { target: { value: "有内容" } });
    fireEvent.keyDown(screen.getByLabelText("回答输入框"), { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("中文 IME 组合态 Enter 不提交", () => {
    const onSubmit = vi.fn();
    render(<StatefulComposer onSubmit={onSubmit} onSkip={() => {}} />);
    fireEvent.change(screen.getByLabelText("回答输入框"), { target: { value: "拼音中" } });
    fireEvent.keyDown(screen.getByLabelText("回答输入框"), { key: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("锁定态（等待期间可见锁定）：textarea/发送/跳过全部禁用", () => {
    render(<StatefulComposer locked onSubmit={() => {}} onSkip={() => {}} />);
    expect((screen.getByLabelText("回答输入框") as HTMLTextAreaElement).disabled).toBeTruthy();
    expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBeTruthy();
    expect((screen.getByRole("button", { name: "跳过此题" }) as HTMLButtonElement).disabled).toBeTruthy();
  });

  it("跳过按钮回调 onSkip；麦克风为禁用占位带 tooltip 文案", () => {
    const onSkip = vi.fn();
    render(<StatefulComposer onSubmit={() => {}} onSkip={onSkip} />);
    fireEvent.click(screen.getByRole("button", { name: "跳过此题" }));
    expect(onSkip).toHaveBeenCalledTimes(1);
    const mic = screen.getByRole("button", { name: "语音输入即将支持" }) as HTMLButtonElement;
    expect(mic.disabled).toBeTruthy();
    expect(screen.getByText("语音输入即将支持")).toBeTruthy();
  });

  it("常驻提示文案", () => {
    render(<StatefulComposer onSubmit={() => {}} onSkip={() => {}} />);
    expect(screen.getByText("Enter 发送 · Shift + Enter 换行")).toBeTruthy();
  });
});
