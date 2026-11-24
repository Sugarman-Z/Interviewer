import type { Metadata } from "next";
import "./globals.css";
import Topbar from "@/components/Topbar";
import SiteFooter from "@/components/SiteFooter";

export const metadata: Metadata = {
  title: {
    default: "陪面 · AI 模拟面试教练",
    template: "%s · 陪面",
  },
  description:
    "输入岗位、简历与 JD，与 AI 面试官多轮问答，按回答质量追问或换题，终场生成综合评估报告。",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body>
 {/* 键盘跳链：首个 tab 位，视觉隐藏、聚焦时出现 */}
        <a className="skipLink" href="#main">
          跳到主要内容
        </a>
        <Topbar />
        <main id="main">{children}</main>
        <SiteFooter />
      </body>
    </html>
  );
}
