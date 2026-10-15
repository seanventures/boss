"use client";

/**
 * The icon a node wears: its built-in step's own glyph when it has one, else one per type.
 *
 * `app/step-icons.tsx` already gives every built-in step a distinct shape and is used as is.
 * The fallbacks cover what it cannot: an agent task, your own steps, gates, waits, nested
 * workflows and the branch blocks. One glyph per type, never shared with a built-in, so a
 * shape always means the same thing on every screen.
 */
import {
  ArrowsClockwise, Circle, Clock, GitBranch, GitFork, GitMerge, ListBullets, PuzzlePiece, Robot, SealCheck,
  Stack,
} from "@phosphor-icons/react";
import React from "react";

import type { StepView } from "../../../lib/workflow-editor-types";
import { hasStepIcon, StepIcon } from "../../step-icons";

type IconType = React.ComponentType<{ size?: number; weight?: "regular" | "bold" | "fill";
                                      "aria-hidden"?: boolean }>;

const TYPE_ICONS: Record<StepView["type"], IconType> = {
  builtin: Circle,
  agent: Robot,
  custom: PuzzlePiece,
  gate: SealCheck,
  wait: Clock,
  subworkflow: Stack,
  parallel: GitFork,
  condition: GitBranch,
  join: GitMerge,
  loop: ArrowsClockwise,
  foreach: ListBullets,
  other: Circle,
};

export function NodeIcon({ icon, type, size = 16 }: { icon?: string; type: StepView["type"]; size?: number }) {
  if (hasStepIcon(icon)) return <StepIcon capability={icon} size={size} />;
  const Icon = TYPE_ICONS[type] ?? Circle;
  return <Icon size={size} weight="bold" aria-hidden />;
}
