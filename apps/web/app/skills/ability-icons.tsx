"use client";

/** The one icon per ability (spec §5.2): Phosphor, named in the kit file. */
import React from "react";
import {
  BookOpen, Books, ChatsCircle, EnvelopeSimple, FileText, GitPullRequest, Globe, Heartbeat,
  MagnifyingGlass, PlugsConnected, RocketLaunch, TestTube, Ticket, YoutubeLogo,
} from "@phosphor-icons/react";

const ICONS = {
  BookOpen, Books, ChatsCircle, EnvelopeSimple, FileText, GitPullRequest, Globe, Heartbeat,
  MagnifyingGlass, RocketLaunch, TestTube, Ticket, YoutubeLogo,
} as const;

export function AbilityIcon({ name, size = 24 }: { name: string; size?: number }) {
  const Icon = (ICONS as Record<string, typeof Globe>)[name] ?? PlugsConnected;
  return <Icon className="kit-icon" size={size} weight="duotone" aria-hidden="true" />;
}
