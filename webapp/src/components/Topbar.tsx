"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import styles from "./topbar.module.css";

const NAV = [
  { href: "/", label: "开始面试", match: (p: string) => p === "/" },
  {
    // 「面试中/面试报告」是上下文页（有具体会话 id 才有意义），入口统一收在会话列表
    href: "/sessions",
    label: "历史会话",
    match: (p: string) =>
      p.startsWith("/sessions") || p.startsWith("/interview") || p.startsWith("/report"),
  },
];

export default function Topbar() {
  const pathname = usePathname();
  return (
    <header className={styles.topbar}>
      <div className={styles.bar}>
        <Link href="/" className={styles.brand} aria-label="陪面 首页">
          <span className={styles.mark} aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              width="18"
              height="18"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="13.5" r="7.5" />
              <path d="M12 10v3.5l2.2 2.2" />
              <path d="M9.5 2.5h5" />
              <path d="M12 2.5v3" />
            </svg>
          </span>
          <span className={styles.name}>陪面</span>
          <span className={styles.sub}>你的随身面试教练</span>
        </Link>
        <nav className={styles.tabs} aria-label="页面导航">
          {NAV.map((item) => {
            const active = item.match(pathname);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={active ? styles.act : styles.tab}
                aria-current={active ? "page" : undefined}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
