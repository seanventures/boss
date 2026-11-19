"use client";

/**
 * ABOUT THIS APP (operator, 2026-11-19: "in the Overview page of the project we need to display the
 * PRD overview and the link to the PRD — if I want to know what this project is and what it built,
 * today I need to navigate to lots of places").
 *
 * The Overview's card, near the top: the founding PRD's one paragraph, its acceptance criteria as a
 * short checklist with the latest acceptance check's verdict on each, who approved the spec and
 * when, "Read the full spec" (the meeting that wrote it) and the specs approved since. A project
 * with no PRD shows its brief and says so. Every word is the server's (engine explained.about).
 */
import React from "react";
import { ArrowRight, BookOpenText, Check, CircleHalf, Minus, SealCheck, X } from "@phosphor-icons/react";

import { criteriaWords, type About, type CriterionStatus } from "../../lib/explained";
import { localWith } from "../../lib/local-time";
import { Ico, PxLink, Tag } from "./kit";
import "./about-app.css";

const MARK: Record<CriterionStatus, { icon: React.ReactNode; word: string; tone: "green" | "amber" | "red" | "idle" }> = {
  met: { icon: <Check size={11} weight="bold" aria-hidden />, word: "Met", tone: "green" },
  partial: { icon: <CircleHalf size={11} weight="fill" aria-hidden />, word: "Partly", tone: "amber" },
  not_met: { icon: <X size={11} weight="bold" aria-hidden />, word: "Not met", tone: "red" },
  unchecked: { icon: <Minus size={11} weight="bold" aria-hidden />, word: "Not checked", tone: "idle" },
};

const day = (iso: string | null) => (iso ? localWith(iso, { month: "short", day: "numeric", year: "numeric" }) : "");

export function AboutCard({ about }: { about: About }) {
  const verdict = criteriaWords(about);
  return (
    <section className="px-card ab-card" aria-labelledby="px-ov-about" data-testid="px-ov-about" data-source={about.source}>
      <div className="px-card-h">
        <Ico icon={BookOpenText} c="violet" />
        <h2 id="px-ov-about">About this app</h2>
        {about.specHref ? (
          <PxLink href={about.specHref} className="px-more" testid="px-ov-about-spec">Read the full spec <ArrowRight size={11} aria-hidden /></PxLink>
        ) : null}
      </div>
      <div className="ab-title" data-testid="px-ov-about-title">{about.title}</div>
      {about.summary ? <p className="ab-summary" data-testid="px-ov-about-summary">{about.summary}</p> : null}
      {about.note ? <p className="ab-note px-faint" data-testid="px-ov-about-note">{about.note}</p> : null}
      {about.approved ? (
        <div className="ab-approved" data-testid="px-ov-about-approved">
          <SealCheck size={13} weight="duotone" aria-hidden />
          <span>{about.approved.words}{about.approved.at ? ` · ${day(about.approved.at)}` : ""}</span>
        </div>
      ) : null}
      {about.criteria.length ? (
        <div className="ab-crit" data-testid="px-ov-about-criteria">
          <div className="ab-crit-h">
            <span className="px-lbl">What it must do</span>
            {verdict ? <span className="ab-verdict" data-testid="px-ov-about-verdict"
                             title={about.checkedAt ? `From the acceptance check on ${day(about.checkedAt)}` : undefined}>{verdict}</span> : null}
          </div>
          <ul className="ab-list">
            {about.criteria.map((c) => {
              const m = MARK[c.status];
              return (
                <li key={c.n} className="ab-item" data-status={c.status} data-testid={`px-ov-about-ac-${c.n}`}
                    title={c.evidence ? `${c.text}\n\n${c.evidence}` : c.text}>
                  <span className="ab-mark" data-tone={m.tone} aria-hidden>{m.icon}</span>
                  <span className="ab-text">{c.text}</span>
                  <Tag tone={m.tone}>{m.word}</Tag>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {about.later.length ? (
        <div className="ab-later" data-testid="px-ov-about-later">
          <span className="px-faint">Changed since:</span>
          {about.later.map((l) => (
            <PxLink key={l.href + l.title} href={l.href} className="px-chip" title={l.approvedAt ? `Approved ${day(l.approvedAt)}` : undefined}>{l.title}</PxLink>
          ))}
          {about.laterCount > about.later.length ? <span className="px-faint">+{about.laterCount - about.later.length} more</span> : null}
        </div>
      ) : null}
    </section>
  );
}
