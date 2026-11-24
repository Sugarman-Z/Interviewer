import type { Metadata } from "next";
import ChatShell from "@/components/chat/ChatShell";

export const metadata: Metadata = { title: "面试中" };

export default async function InterviewPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  // key：同路由换会话 id 时强制重挂载，避免旧会话状态残留到新 URL 下
  return <ChatShell key={sessionId} sessionId={sessionId} />;
}
