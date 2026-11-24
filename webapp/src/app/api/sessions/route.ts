/**
 * POST /api/sessions —— 创建会话（Mock）
 * GET  /api/sessions —— 会话列表（Mock）
 * 支持 JSON 与 multipart（带简历文件）两种请求体；岗位为空 400（引擎「防捏造第一闸」对应）。
 */
import { NextResponse, type NextRequest } from "next/server";
import { createSession, listSummaries, sleepMockDelay } from "@/lib/mock/store";
import { MAX_QUESTIONS } from "@/lib/mock/script";
import { POSITION_REQUIRED_MSG } from "@/lib/messages";
import type { CreateSessionInput, SessionCreated } from "@/lib/types";

export async function POST(req: NextRequest) {
  await sleepMockDelay();
  let input: CreateSessionInput;
  const contentType = req.headers.get("content-type") ?? "";
  if (contentType.includes("multipart/form-data")) {
    const fd = await req.formData().catch(() => null);
    if (!fd) {
      return NextResponse.json({ error: "请求体不是合法的表单" }, { status: 400 });
    }
    // multipart 的每个部件都可能是 File，先判类型再取值（File 被 String() 会成 "[object File]"）
    const pos = fd.get("position");
    const jd = fd.get("jd");
    const resumeText = fd.get("resume_text");
    const file = fd.get("resume_file");
    input = {
      position: typeof pos === "string" ? pos.trim() : "",
      jd: typeof jd === "string" && jd ? jd : undefined,
      resume_text:
        file instanceof File
          ? `（已上传简历文件：${file.name}，${file.size} 字节 —— Mock 不解析内容，真实后端将提取文本）`
          : typeof resumeText === "string" && resumeText
            ? resumeText
            : undefined,
    };
  } else {
    const body = (await req.json().catch(() => null)) as
      | { position?: unknown; jd?: unknown; resume_text?: unknown }
      | null;
    input = {
      position: String(body?.position ?? "").trim(),
      jd: typeof body?.jd === "string" && body.jd ? body.jd : undefined,
      resume_text:
        typeof body?.resume_text === "string" && body.resume_text ? body.resume_text : undefined,
    };
  }
  if (!input.position) {
    return NextResponse.json({ error: POSITION_REQUIRED_MSG }, { status: 400 });
  }
  const s = createSession(input);
  if (!s.pending_question) {
    return NextResponse.json({ error: "会话创建异常：首问缺失" }, { status: 500 });
  }
  // satisfies 让编译器盯着响应形状 = 契约类型（单一定义点）
  return NextResponse.json(
    {
      session_id: s.session_id,
      max_questions: MAX_QUESTIONS,
      question: s.pending_question,
    } satisfies SessionCreated,
    { status: 201 },
  );
}

export async function GET() {
  return NextResponse.json(listSummaries());
}
