import "./globals.css";
import "./design-system.css";
// Command Center, area loading, approval guidance and action results — kept out of globals.css
// so these screens do not churn the stylesheet every console change touches.
import "./command-center.css";
// The Command Center bridge (page layout) and its graphics (Floor, Today, day strip, week).
import "./command-center/bridge.css";
import "./command-center/graphics.css";
// Workflow Studio (library, editor, lane, inspector), one file per surface.
import "./workflows/flow.css";
import "./workflows/editor.css";
import "./workflows/inspector.css";
import "./workflows/library.css";
// New project: a new app, or a feature added to an app that exists (np- classes).
import "./workflows/compose.css";
// Agent World (avatars, motion, Passport, Roster, Chain, conversations): ag- classes, agav- keyframes.
import "./agents/agent-world.css";
import "./setup/setup.css";
import "./skills/kit.css";
// Factory (Floor, Line, Apps, the cockpit): fx- classes.
import "./factory/factory.css";
import "./factory/cockpit.css";
import "./activity/log.css";
import "./govern/govern.css";
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
