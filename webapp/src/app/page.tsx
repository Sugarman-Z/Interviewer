import type { Metadata } from "next";
import Wizard from "@/components/wizard/Wizard";

export const metadata: Metadata = {
  // root layout 的 template 不作用于同层的 page（Next 文档明示），absolute 保持品牌后缀一致
  title: { absolute: "开始一场陪练 · 陪面" },
};

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ position?: string }>;
}) {
  // 「再来一场」从报告页带岗位回来预填；页头与 680 列在 Wizard 内
  //（与 SessionList/ReportShell 同惯例：列类与 shell 叠同一元素，margin auto 居中）
  const { position } = await searchParams;
  return <Wizard initialPosition={position ?? ""} />;
}
