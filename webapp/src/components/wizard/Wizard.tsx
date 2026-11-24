"use client";

/**
 * 三步输入向导：岗位必填 → 简历可选（上传/粘贴互斥）→ JD 可选 + 汇总条。
 * 提交 POST /api/sessions → 跳 /interview/[id]；失败原位报错，不清空已填内容。
 */
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, createSession, withTimeoutRetry } from "@/lib/api";
import ResumeStep from "./ResumeStep";
import {
  POSITION_CHIPS,
  POSITION_REQUIRED_MSG,
  summarize,
  validatePosition,
} from "./wizardLogic";
import s from "./wizard.module.css";

/** §10-6 最小韧性：单次超时 + 失败立即重试一次 */
const CREATE_TIMEOUT_MS = 30_000;

const STEPS = [
  { label: "选择岗位", optional: false },
  { label: "简历", optional: true },
  { label: "职位描述", optional: true },
] as const;

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

export default function Wizard({ initialPosition = "" }: { initialPosition?: string }) {
  const router = useRouter();
  const posRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState(1);
  const [position, setPosition] = useState(initialPosition);
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [resumeText, setResumeText] = useState("");
  const [jd, setJd] = useState("");
  const [posErr, setPosErr] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr] = useState<string | null>(null);
  /** 简历双轨切换提示：互斥清除另一轨时告知用户（"file"=刚切到上传，"paste"=刚切到粘贴） */
  const [switched, setSwitched] = useState<"file" | "paste" | null>(null);

  // 换步后把焦点移进新面板：键盘/读屏用户不丢焦点位置（首屏挂载不抢焦点）
  const panelRef = useRef<HTMLDivElement>(null);
  const lastStepRef = useRef(step);
  useEffect(() => {
    if (lastStepRef.current === step) return;
    lastStepRef.current = step;
    panelRef.current?.focus();
  }, [step]);

  const next = () => {
    if (step === 1 && validatePosition(position)) {
      setPosErr(true);
      posRef.current?.focus();
      return;
    }
    setStep((n) => Math.min(3, n + 1));
  };

  const submit = async () => {
    setSubmitting(true);
    setSubmitErr(null);
    try {
      // §10-6 最小韧性：30s 超时 + 失败立即重试一次
      const created = await withTimeoutRetry(
        (sig) =>
          createSession(
            {
              position: position.trim(),
              jd: jd.trim() || undefined,
              resumeText: resumeText.trim() || undefined,
              resumeFile: resumeFile ?? undefined,
            },
            sig,
          ),
        CREATE_TIMEOUT_MS,
      );
      router.push(`/interview/${created.session_id}`);
    } catch (e) {
      setSubmitErr(
        e instanceof ApiError ? e.message : "创建会话失败，请检查网络后重试",
      );
      setSubmitting(false);
    }
  };

  const summaryRows = summarize(position, resumeFile, resumeText, jd);

  return (
    <div className={`shell ${s.wcol}`}>
      {/* 页头收在 680 列内随列居中——shell 与列类叠同一元素（同 SessionList/ReportShell），
          设计稿 direction-b 的 wrap+wcol 本就是同一元素，移植拆两层导致列贴左（已修） */}
      <div className="page-head">
        <h1>开始一场陪练</h1>
        <p>三步准备，教练会结合岗位和材料为你定制问题</p>
      </div>
      <div className={`card ${s.wcard}`}>
        <ol className={s.steps}>
          {STEPS.map((st, i) => {
            const n = i + 1;
            const state = n === step ? s.cur : n < step ? s.done : "";
            return (
              <li key={st.label} className={`${s.step} ${state}`}>
                {n < step ? (
                  <button
                    type="button"
                    className={s.stepBtn}
                    disabled={submitting}
                    onClick={() => setStep(n)}
                    aria-label={`回到第 ${n} 步：${st.label}`}
                  >
                    <span className={s.dot}>
                      <CheckIcon />
                    </span>
                  </button>
                ) : (
                  <span className={s.dot} aria-current={n === step ? "step" : undefined}>
                    {n}
                  </span>
                )}
                <span className={s.slabel}>
                  {st.label}
                  {st.optional && <small>可选</small>}
                </span>
              </li>
            );
          })}
        </ol>
        {/* 换步播报（读屏）；视觉隐藏 */}
        <p className={s.srOnly} aria-live="polite">
          第 {step} 步（共 3 步）：{STEPS[step - 1].label}
        </p>

        {step === 1 && (
          <div
            className={s.wpanel}
            key="p1"
            ref={panelRef}
            tabIndex={-1}
            role="group"
            aria-label={`第 1 步：选择岗位`}
          >
            <label className={s.fLabel} htmlFor="pos">
              目标岗位 <em>*</em>
            </label>
            <input
              id="pos"
              ref={posRef}
              className={s.inp}
              placeholder="例如：后端开发工程师"
              autoComplete="off"
              value={position}
              onChange={(e) => {
                setPosition(e.target.value);
                if (posErr) setPosErr(false);
              }}
              onKeyDown={(e) => {
                if (e.key !== "Enter") return;
                // 中文 IME 用 Enter 上屏候选词，不算「下一步」
                if (e.nativeEvent.isComposing) return;
                e.preventDefault();
                next();
              }}
              aria-invalid={posErr || undefined}
              aria-describedby={posErr ? "pos-err" : undefined}
            />
            <p className={s.fHint}>不确定怎么写？点一个常见岗位直接填入</p>
            <div className={s.chips}>
              {POSITION_CHIPS.map((c) => (
                <button
                  key={c}
                  type="button"
                  className={position === c ? s.on : undefined}
                  aria-pressed={position === c}
                  onClick={() => {
                    setPosition(c);
                    setPosErr(false);
                  }}
                >
                  {c}
                </button>
              ))}
            </div>
            {posErr && (
              <p className={s.fErr} id="pos-err" role="alert">
                {POSITION_REQUIRED_MSG}
              </p>
            )}
            <div className={s.wbtns}>
              <span />
              <button type="button" className="btn btn-pri" onClick={next}>
                下一步 <ArrowIcon />
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div
            className={s.wpanel}
            key="p2"
            ref={panelRef}
            tabIndex={-1}
            role="group"
            aria-label={`第 2 步：简历（可选）`}
          >
            <ResumeStep
              file={resumeFile}
              text={resumeText}
              onFile={(f) => {
                setResumeFile(f);
                if (f) {
                  if (resumeText.trim()) setSwitched("file");
                  setResumeText(""); // 互斥：选了文件清空粘贴
                } else {
                  setSwitched(null); // × 移除不算切换
                }
              }}
              onText={(t) => {
                setResumeText(t);
                if (t.trim() && resumeFile) {
                  setResumeFile(null); // 互斥：粘贴即弃文件
                  setSwitched("paste");
                }
              }}
            />
            {switched === "paste" && (
              <p className={s.switchNote}>已改用粘贴文本——原先选择的简历文件已移除</p>
            )}
            {switched === "file" && (
              <p className={s.switchNote}>已改用上传文件——原先粘贴的文本已清空</p>
            )}
            <div className={s.wbtns}>
              <button type="button" className="btn btn-ghost" onClick={() => setStep(1)}>
                上一步
              </button>
              <button type="button" className="btn btn-pri" onClick={next}>
                下一步 <ArrowIcon />
              </button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div
            className={s.wpanel}
            key="p3"
            ref={panelRef}
            tabIndex={-1}
            role="group"
            aria-label={`第 3 步：职位描述（可选）`}
          >
            <label className={s.fLabel} htmlFor="jd">
              职位描述（JD）<em className={s.optNote}>可选</em>
            </label>
            <textarea
              id="jd"
              className={`${s.tin} ${s.jdTin}`}
              placeholder={
                "把招聘 JD 粘贴到这里，教练会把问题贴向这家公司的考察点…\n\n例如：负责高并发订单系统的设计与优化，熟悉缓存、消息队列、分布式锁……"
              }
              value={jd}
              onChange={(e) => setJd(e.target.value)}
            />
            <div className={s.sum}>
              {summaryRows.map((r) => (
                <div className={s.sumR} key={r.label}>
                  <span>{r.label}</span>
                  <b>{r.value}</b>
                </div>
              ))}
            </div>
            {submitErr && (
              <div className={s.errBanner} role="alert">
                {submitErr}
              </div>
            )}
            <div className={s.wbtns}>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={submitting}
                onClick={() => setStep(2)}
              >
                上一步
              </button>
              <button
                type="button"
                className="btn btn-pri btn-big"
                disabled={submitting}
                onClick={() => void submit()}
              >
                {submitting ? "正在开始…" : "开始面试"} <ArrowIcon />
              </button>
            </div>
          </div>
        )}
      </div>
      <p className={s.footNote}>
        <LockIcon /> 简历与 JD 只用于本次出题，会话结束即可删除
      </p>
    </div>
  );
}
