import "./globals.css";
import "./design-system.css";
// Command Center, area loading, approval guidance and action results — kept out of globals.css
// so these screens do not churn the stylesheet every console change touches.
import "./command-center.css";
// Workflow Studio (library, editor, lane, inspector), one file per surface.
import "./workflows/flow.css";
import "./workflows/editor.css";
import "./workflows/inspector.css";
import "./workflows/library.css";
// Agent World (avatars, motion, Passport, Roster, Chain, conversations): ag- classes, agav- keyframes.
import "./agents/agent-world.css";
import "./skills/kit.css";
import type { Metadata } from "next";
import { Toaster } from "./client-forms";

export const metadata: Metadata = {
  title: "BOSS Executive Console",
  description: "Business Operating System Sentinel",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        {children}
        <Toaster />
      </body>
    </html>
  );
}
