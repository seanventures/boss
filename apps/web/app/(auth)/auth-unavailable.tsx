"use client";

import React, { useTransition } from "react";
import { useRouter } from "next/navigation";

import { requestConsoleRefresh } from "../../lib/console-refresh";
import { AuthStage, Live, Logo } from "./auth-parts";

/** Unknown identity state offers only another read, never a claim or sign-in mutation. */
export function AuthUnavailable({ host }: { host: string }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  return (
    <AuthStage label="Sign-in unavailable">
      <Logo host={host} />
      <h1>Sign-in unavailable</h1>
      <p className="idn-s" role="alert" data-copy="prose">
        BOSS can&apos;t load sign-in or setup right now. We couldn&apos;t check whether this BOSS has an Owner.
      </p>
      <button type="button" className="idn-big p" disabled={pending}
        onClick={() => startTransition(() => requestConsoleRefresh(router, "now"))}>
        {pending ? "Retrying…" : "Retry"}
      </button>
      <Live text={pending ? "Retrying" : ""} />
    </AuthStage>
  );
}
