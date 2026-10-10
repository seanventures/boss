"use client";

/**
 * One icon per kind of step, so a rail of ten nodes is scannable without reading it.
 *
 * Every node looked identical apart from a number and a tick, which means finding "the deploy"
 * on a ten-step run is a reading task rather than a glance. Shape is faster than text for
 * exactly this: you learn the deploy is the rocket once and never read that node again.
 *
 * Chosen so that no two steps share a glyph — the point is telling them apart, and a shared
 * icon would be worse than none, since it would assert a similarity that does not exist. Only
 * names this codebase already imports elsewhere, so the set cannot drift from what is installed.
 */
import {
  ArrowsClockwise,
  Blueprint,
  ChatsCircle,
  CheckCircle,
  Cloud,
  FileText,
  Flask,
  Gauge,
  Heartbeat,
  ListMagnifyingGlass,
  MagnifyingGlass,
  PaintBrush,
  PaperPlaneTilt,
  Play,
  PlayCircle,
  Rocket,
  ShieldCheck,
  Ticket,
  Trash,
  Wallet,
  Wrench,
} from "@phosphor-icons/react";
import React from "react";

type IconType = React.ComponentType<{ size?: number; weight?: "regular" | "bold" | "fill";
                                      "aria-hidden"?: boolean }>;

const STEP_ICONS: Record<string, IconType> = {
  research: ListMagnifyingGlass,       // reading the world before building in it
  meeting: ChatsCircle,                // a panel arguing to a PRD
  spec_review: CheckCircle,            // a person signing the spec off
  architecture: Blueprint,             // how it is put together, signed off before code
  build: Wrench,                       // the inner pipeline doing the work
  review: MagnifyingGlass,             // reading the diff adversarially
  test: Flask,                         // proving it
  preview: PlayCircle,                 // somebody using it
  ux_review: PaintBrush,               // how it looks and feels
  security_review: ShieldCheck,        // the lens the step exists for
  plrr: Gauge,                         // readiness, measured
  deploy: Rocket,                      // onto a machine
  monitor: Heartbeat,                  // still alive
  notify: PaperPlaneTilt,              // telling somebody
  smoke_test: Play,                    // does the shipped thing answer
  redeploy: Cloud,                     // the same destination, again
  retry_build: ArrowsClockwise,        // round again
  teardown_preview: Trash,             // reclaiming what it held
  cost_check: Wallet,                  // what it cost
  document: FileText,                  // the written record
  agents: Ticket,                      // who is on it
};

/** The icon for a step kind, or nothing — a step BOSS has no glyph for gets its number. */
export function StepIcon({ capability, size = 13 }: { capability?: string; size?: number }) {
  const Icon = capability ? STEP_ICONS[capability] : undefined;
  if (!Icon) return null;
  return <Icon size={size} weight="bold" aria-hidden />;
}

/** Whether a kind has its own glyph, so a caller can fall back to the step number. */
export function hasStepIcon(capability?: string): boolean {
  return !!capability && !!STEP_ICONS[capability];
}
