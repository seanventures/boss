/** A stack card's three answers, posted through the approve and reject routes that already exist.

Accept alt continues the build (approve). Enable toolchain and Stop do not (reject). The note
names which one, so the two refusals stay different on the decision. */
export const STACK_CHOICES = ["enable_toolchain", "accept_alt", "stop"] as const;

export type StackChoice = (typeof STACK_CHOICES)[number];

const STACK_HTTP = {
  accept_alt: "approve",
  enable_toolchain: "reject",
  stop: "reject",
} as const;

export function stackButtonPost(action: string, note: string): {
  action: "approve" | "reject";
  note: string;
} | null {
  if (!(action in STACK_HTTP)) return null;
  const choice = action as StackChoice;
  const prefix = `Stack choice: ${choice}.`;
  const said = note.trim();
  return { action: STACK_HTTP[choice], note: said ? `${prefix} ${said}` : prefix };
}
