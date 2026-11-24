"use client";

import { usePathname } from "next/navigation";

export default function SiteFooter() {
  const pathname = usePathname();
  // 面试聊天页需要占满视口（底部固定输入区），不渲染页脚
  if (pathname?.startsWith("/interview/")) return null;
  return (
    <footer className="foot">
      陪面 Web 版开发中 · 会话数据仅存本机
    </footer>
  );
}
