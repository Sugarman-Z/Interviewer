"use client";

/**
 * 向导第 2 步：简历上传 / 粘贴双轨（互斥，二选一）。
 * 文件与文本的状态归 Wizard 持有；本组件只负责选择交互与文件校验展示。
 */
import { useEffect, useRef, useState } from "react";
import { formatBytes, validateResumeFile } from "./wizardLogic";
import s from "./wizard.module.css";

interface ResumeStepProps {
  file: File | null;
  text: string;
  onFile: (file: File | null) => void;
  onText: (text: string) => void;
}

function PaperclipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21.4 11.05 12.9 19.6a5 5 0 0 1-7.1-7.1l8.5-8.5a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-2.9-2.9l7.8-7.7" />
    </svg>
  );
}

function ClipboardIcon() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
      <rect x="9" y="3" width="6" height="4" rx="1" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 16V4M7 9l5-5 5 5" />
      <path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}

export default function ResumeStep({ file, text, onFile, onText }: ResumeStepProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [fileErr, setFileErr] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // 窗口级兜底：文件拖偏出投放区时拦掉浏览器默认行为（否则会直接打开文件，丢掉整个表单）
  useEffect(() => {
    const prevent = (e: DragEvent) => {
      if (e.dataTransfer?.types.includes("Files")) e.preventDefault();
    };
    window.addEventListener("dragover", prevent);
    window.addEventListener("drop", prevent);
    return () => {
      window.removeEventListener("dragover", prevent);
      window.removeEventListener("drop", prevent);
    };
  }, []);

  const pick = (files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    const err = validateResumeFile(f);
    if (err) {
      setFileErr(err);
      return;
    }
    setFileErr(null);
    onFile(f);
  };

  return (
    <>
      <div className={s.fLabel}>
        简历 <em className={s.optNote}>可选 · 提供后教练会优先围绕你的项目出题</em>
      </div>
      <div className={s.rgrid}>
        <div className={s.ropt}>
          <div className={s.roptT}>
            <PaperclipIcon /> 上传文件
          </div>
          <p className={s.roptD}>支持 .txt / .md / .pdf / .docx，最大 10 MB</p>
          <input
            ref={inputRef}
            type="file"
            accept=".txt,.md,.pdf,.docx"
            hidden
            onChange={(e) => {
              pick(e.target.files);
              e.target.value = ""; // 允许再次选择同名文件
            }}
          />
          {file ? (
            <div className={s.fok}>
              <FileIcon />
              <div className={s.fokT}>
                <b>{file.name}</b>
                <span>{formatBytes(file.size)} · 提交后上传，由教练解析你的项目经历</span>
              </div>
              <button
                type="button"
                className={s.fx}
                aria-label="移除简历文件"
                onClick={() => {
                  setFileErr(null);
                  onFile(null);
                }}
              >
                ×
              </button>
            </div>
          ) : (
            <button
              type="button"
              className={`${s.drop} ${dragOver ? s.over : ""}`}
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                pick(e.dataTransfer.files);
              }}
            >
              <UploadIcon />
              <b>点击或拖拽简历到这里</b>
              <span>解析后教练会优先围绕你的项目经历出题</span>
            </button>
          )}
          {fileErr && (
            <p className={s.fErr} role="alert">
              {fileErr}
            </p>
          )}
        </div>

        <div className={s.or}>或</div>

        <div className={s.ropt}>
          <div className={s.roptT}>
            <ClipboardIcon /> 粘贴文本
          </div>
          <p className={s.roptD}>直接把简历内容贴进来也可以</p>
          <textarea
            className={s.tin}
            aria-label="粘贴简历文本"
            placeholder={"把简历文本粘贴到这里…\n\n技能、项目经历越具体，教练的提问越贴身"}
            value={text}
            onChange={(e) => {
              setFileErr(null); // 切到粘贴轨道，文件校验的错误不再相关
              onText(e.target.value);
            }}
          />
          <div className={`${s.fHint} ${s.taR}`}>{text.trim().length} 字</div>
        </div>
      </div>
      {!file && !text.trim() && (
        <p className={s.noneHint}>不提供也可以——本场面试将仅依据岗位与你的自我介绍进行</p>
      )}
    </>
  );
}
