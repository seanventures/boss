"use client";

/**
 * THE ONE LINK TO THE CHANGE A RUN PRODUCED (BOSSAPP-994).
 *
 * Every work-stream, run and build card that produced a change names it with this: "Change #3"
 * for a local change set, "PR #3" for a GitHub pull request, linking to BOSS's change page
 * (`changeHref`). The cards used to spell this out one by one, and three of them said "PR #N"
 * to a project with no GitHub anywhere while the Floor's build rows said nothing at all — the
 * operator read the link as buried or missing. The words and the destination live in
 * `lib/repo-links.ts`; this is the one place a card turns them into a link, so a new card
 * cannot get them wrong (`tests/every-card-names-its-change.test.tsx` holds the line).
 */
import React from "react";
import { GitMerge, GitPullRequest } from "@phosphor-icons/react";

import { changeHref, changeRef, isLocalChangeUrl, linkTarget } from "../lib/repo-links";

export function changeTip(url: string | null | undefined, merged = false): string {
  if (isLocalChangeUrl(url)) return merged ? "Merged change set in BOSS" : "Change set in BOSS";
  return merged ? "Merged pull request" : "Pull request";
}

/** "Change #3" / "PR #3" as a link to the change page. `number` alone (no URL recorded) renders
 *  the words without a link rather than a guessed destination; nothing renders without a number.
 *  `merged` draws the merge glyph; `icon={false}` leaves the words bare for dense rows. */
export function ChangeLink({ url, number, className, testid, merged, icon = true, title }: {
  url: string | null | undefined;
  number: number | string | null | undefined;
  className?: string;
  testid?: string;
  merged?: boolean;
  icon?: boolean;
  title?: string;
}) {
  if (!number) return null;
  const words = changeRef(url, number);
  const glyph = icon ? (merged ? <GitMerge size={11} aria-hidden /> : <GitPullRequest size={11} aria-hidden />) : null;
  const href = changeHref(url);
  if (!href) return <span className={className} data-testid={testid} title={title ?? changeTip(url, merged)}>{glyph}{glyph ? " " : ""}{words}</span>;
  return (
    <a className={className ?? "ag-link"} href={href} {...linkTarget(url)} data-testid={testid}
       title={title ?? changeTip(url, merged)} onClick={(e) => e.stopPropagation()}>
      {glyph}{glyph ? " " : ""}{words}
    </a>
  );
}
