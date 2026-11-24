import type { Metadata } from "next";
import ReportShell from "@/components/report/ReportShell";

export const metadata: Metadata = { title: "面试报告" };

export default async function ReportPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  return <ReportShell sessionId={sessionId} />;
}
