import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "DevInsight AI · 科技产业情报",
  description: "持续采集中国与全球公开数据源，把原始信息转化为可检索、可追溯的情报。",
};

const NAV = [
  { href: "/intelligence", label: "情报流" },
  { href: "/sources", label: "数据源" },
] as const;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
        <header className="border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
          <div className="mx-auto flex h-14 max-w-6xl items-center gap-8 px-6">
            <Link href="/intelligence" className="flex items-center gap-2 font-semibold tracking-tight">
              <span className="grid size-6 place-items-center rounded-md bg-zinc-900 text-[11px] font-bold text-white dark:bg-zinc-100 dark:text-zinc-900">
                D
              </span>
              DevInsight AI
            </Link>
            <nav className="flex items-center gap-1 text-sm">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="rounded-md px-3 py-1.5 text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <span className="ml-auto hidden text-xs text-zinc-400 sm:block">
              多源情报 · 事实抽取与时间线
            </span>
          </div>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">{children}</main>
        <footer className="border-t border-zinc-200 py-6 text-center text-xs text-zinc-400 dark:border-zinc-800">
          仅保存标题、摘要与原文链接，全文归属原发布方
        </footer>
      </body>
    </html>
  );
}
