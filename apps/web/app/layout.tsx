import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "BOSS Executive Console",
  description: "Business Operating System Sentinel",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

