import type { Metadata } from "next";
import SessionList from "@/components/sessions/SessionList";

export const metadata: Metadata = { title: "历史会话" };

export default function SessionsPage() {
  return <SessionList />;
}
