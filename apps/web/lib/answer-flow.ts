/**
 * AN ANSWER THAT OPENS WITH A DIAGRAM (screen 09: "Merge PR #3 → Search filters → Staging →
 * Demo ready"). A captain's status answer may lead with one line of 2–6 steps joined by "→";
 * the chat draws it as flow chips and keeps the rest as the answer. Each step may carry a mark:
 *   ✓ done · ● running now · ! needs you · (none) not started
 * A line that is not such a chain (too few or too long steps, prose with an arrow in it) is left
 * as text: the diagram is only drawn when every step reads as a label (≤ 4 words).
 */
export type FlowState = "done" | "now" | "waiting" | "todo";
export type AnswerFlowNode = { label: string; state: FlowState };

const MARKS: [RegExp, FlowState][] = [
  [/^(?:✓|✔|\[x\]|\[done\])\s*/i, "done"],
  [/^(?:●|•|⏳|\[now\]|\[running\])\s*/i, "now"],
  [/^(?:!|⚠|\[you\])\s*/i, "waiting"],
];

function node(raw: string): AnswerFlowNode | null {
  let text = raw.replace(/\*\*/g, "").replace(/`/g, "").trim();
  let state: FlowState = "todo";
  for (const [re, s] of MARKS) {
    if (re.test(text)) { state = s; text = text.replace(re, "").trim(); break; }
  }
  const words = text.split(/\s+/).filter(Boolean);
  if (!words.length || words.length > 4 || text.length > 32) return null;
  return { label: text, state };
}

/** The answer's leading flow line, if it has one, and the text without it. */
export function leadingFlow(text: string): { nodes: AnswerFlowNode[]; rest: string } {
  const body = String(text ?? "");
  const lines = body.split("\n");
  const first = lines.findIndex((l) => l.trim() !== "");
  if (first < 0) return { nodes: [], rest: body };
  const line = lines[first].trim();
  if (!line.includes("→")) return { nodes: [], rest: body };
  const parts = line.split("→");
  if (parts.length < 2 || parts.length > 6) return { nodes: [], rest: body };
  const nodes = parts.map(node);
  if (nodes.some((n) => n === null)) return { nodes: [], rest: body };
  return { nodes: nodes as AnswerFlowNode[], rest: lines.slice(first + 1).join("\n").trim() };
}
