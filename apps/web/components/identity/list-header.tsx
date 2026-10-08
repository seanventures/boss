/**
 * A LIST'S CREATE ACTION LIVES IN EXACTLY ONE PLACE.
 *
 * Identity › Groups showed two "New group" buttons a few pixels apart: one in the header, one in
 * the empty state. Every list had grown its own pairing, and three of them doubled (Groups, API
 * keys, People) while one tripled (Service accounts). The rule, owned here:
 *
 *   - the list is EMPTY: the action sits in the empty state, which says what the thing is for;
 *   - otherwise: the action sits in the header, beside the count.
 *
 * Pass the same `create` element to <ListHeader> and to the <EmptyState action>; the header drops
 * it while the list is empty. tests/identity-one-create-action.test.tsx walks every list.
 */
import React, { type ReactNode } from "react";

export function ListHeader({ level = 1, id, title, sub, empty, create, children }: {
  /** 1: the page's own heading (People, Groups); 2: a card on a page (API keys, Keys). */
  level?: 1 | 2 | 3;
  id?: string;
  title: ReactNode;
  sub?: ReactNode;
  /** The list has nothing in it, so its empty state carries `create`. */
  empty: boolean;
  /** The primary create action (New group, Invite people, New key). */
  create?: ReactNode;
  /** Secondary actions (Export, Import): shown whether or not the list is empty. */
  children?: ReactNode;
}) {
  const H = level === 1 ? "h1" : level === 2 ? "h2" : "h3";
  return (
    <div className={level === 1 ? "idn-sec-h" : "idn-card-h"} data-list-header>
      <H id={id} className={level === 1 ? "idn-h1" : undefined}>{title}</H>
      {sub != null && sub !== "" ? <span className="idn-sub">{sub}</span> : null}
      <span className="idn-spacer" />
      {children}
      {empty ? null : create}
    </div>
  );
}
