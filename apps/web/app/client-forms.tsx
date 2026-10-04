'use client';

import React, { useEffect, useRef, useState, useTransition, ReactNode } from 'react';
import { useConsoleRefresh } from "./use-console-refresh";
import { useFormStatus } from 'react-dom';

import { describeActionError, isActionResult, type ActionResult } from '../lib/action-result';

import { pasteConnectSuccessCopy } from '../lib/paste-connect';
import { notifyChanged } from './changed';
import {
  createKpiAction,
  submitCommandAction,
  createProviderAccountAction,
  easyConnectProviderAction,
  queueOutboxMessageAction,
  testProviderAccountAction,
  syncProviderResourcesAction,
  applyChangeSetAction,
  resolveDecisionAction,
} from './actions';

import { callAction } from "../lib/call-action";
export type ToastType = 'success' | 'error';

interface Toast {
  id: number;
  type: ToastType;
  message: string;
}

export function showToast(type: ToastType, message: string): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent('boss:toast', {
        detail: { type, message },
      }),
    );
  }
}

export function Toaster(): React.ReactElement | null {
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    function onToast(e: Event) {
      const detail = (e as CustomEvent<{ type: ToastType; message: string }>).detail;
      const id = Date.now() + Math.random();
      const toast: Toast = { id, type: detail.type, message: detail.message };
      setToasts((prev) => [...prev, toast]);
      // auto-dismiss
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 4800);
    }
    window.addEventListener('boss:toast', onToast as EventListener);
    return () => window.removeEventListener('boss:toast', onToast as EventListener);
  }, []);

  if (toasts.length === 0) {
    return null;
  }

  return (
    <div
      className="toast-container"
      role="region"
      aria-label="Status notifications"
      aria-live="polite"
      aria-atomic="true"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast toast-${t.type}`}
          role={t.type === 'error' ? 'alert' : 'status'}
          aria-live={t.type === 'error' ? 'assertive' : 'polite'}
          aria-atomic="true"
        >
          <span>{t.message}</span>
          <button
            type="button"
            onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))}
            aria-label="Dismiss notification"
            className="toast-dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

type FormAttributes = Omit<React.FormHTMLAttributes<HTMLFormElement>, "action" | "children">;

interface ActionFormProps extends FormAttributes {
  /** A server action. Returns an ActionResult (lib/action-result.ts); a legacy void return
   *  is read as success, and a throw as failure. */
  action: (formData: FormData) => Promise<ActionResult | void>;
  /** The toast on success when the action does not say something more specific itself. */
  successMessage?: string;
  /** Called with the result, for a form that has more to do than show it (open a run, say). */
  onResult?: (result: ActionResult) => void;
  children: ReactNode;
  'data-testid'?: string;
}

/** While the action runs. Read from inside the form, so every form gets it with no wiring. */
function ActionFormPending(): React.ReactElement | null {
  const status = useFormStatus();
  if (!status.pending) return null;
  return (
    <p className="loading action-form-pending" role="status" aria-live="polite" aria-atomic="true">
      Working…
    </p>
  );
}

/**
 * THE FORM EVERY SERVER ACTION GOES THROUGH.
 *
 * A raw `<form action={serverAction}>` had three ways to tell the operator nothing: a thrown
 * error became Next's redacted "error occurred in the Server Components render" (no error
 * boundary exists, so the page crashed), a refused request the action never checked looked
 * exactly like success, and a success changed the page somewhere below the fold with no word
 * that it had. This renders the action's ActionResult: pending while it runs, then a toast and
 * an inline line — the sentence from the server on failure, with what to do about it.
 *
 * tests/server-actions-return-results.test.ts fails on any `<form action={xAction}>` that does
 * not come through here.
 */
export function ActionForm({
  action,
  successMessage,
  onResult,
  children,
  ...rest
}: ActionFormProps): React.ReactElement {
  const [result, setResult] = useState<ActionResult | null>(null);

  // An async form action: React holds the form's pending state until it settles, which is
  // what SubmitButton and ActionFormPending read.
  const handleAction = async (formData: FormData) => {
    setResult(null);
    let outcome: ActionResult;
    try {
      // Through callAction, so the press reaches the server and the action's API writes are
      // keyed by it: these forms submit with a plain button, not a SubmitButton.
      const returned = await callAction(action)(formData);
      outcome = isActionResult(returned) ? returned : { ok: true };
    } catch (err: unknown) {
      outcome = { ok: false, error: describeActionError(err) };
    }
    setResult(outcome);
    onResult?.(outcome);
    if (outcome.ok) {
      showToast('success', outcome.message ?? successMessage ?? 'Done.');
      // The action revalidated on the server, and its reply carries the new page. The signal
      // tells every panel that polls on its own to reload too (see changed.ts). Not useRouter:
      // ActionForm renders inside server components that are also rendered outside the app
      // router (tests, the org gate), where useRouter throws.
      notifyChanged('action-form');
    } else {
      showToast('error', outcome.error ?? 'That did not work, and the server gave no reason.');
    }
  };

  return (
    <form {...rest} action={handleAction}>
      {children}
      <ActionFormPending />
      {result && !result.ok ? (
        <p className="error-banner" role="alert" aria-atomic="true" data-testid="action-form-error">
          {result.error}
        </p>
      ) : null}
      {result && result.ok && (result.message || result.href) ? (
        <p className="action-form-done" role="status" aria-live="polite"
           data-testid="action-form-done">
          {result.message ?? successMessage}
          {result.href ? (
            <>
              {" "}
              <a href={result.href}>{result.hrefLabel ?? "Open it"}</a>
            </>
          ) : null}
        </p>
      ) : null}
    </form>
  );
}

// Convenience wrappers for the 5+ key actions (KPI, command, provider, self-imp, budget via outbox).
// Each provides action-specific success copy per ux-review P2 + DoD (loading via pending, success toast + revalidate, error banner + toast).

export function KpiCreateForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={createKpiAction}
      successMessage="KPI saved. Registry revalidated; event feed updated (kpi.upserted)."
      className={className}
      data-testid="kpi-create-form"
    >
      {children}
    </ActionForm>
  );
}

export function DecisionResolveForm({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <ActionForm
      action={resolveDecisionAction}
      successMessage="Decision recorded. Inbox revalidated."
      className={className}
      data-testid="decision-resolve-form"
    >
      {children}
    </ActionForm>
  );
}

export function CommandSubmitForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={submitCommandAction}
      successMessage="Command submitted. Task/plan created + planning job leased (budget gated)."
      className={className}
      data-testid="command-submit-form"
    >
      {children}
    </ActionForm>
  );
}

export function ProviderCreateForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={createProviderAccountAction}
      successMessage="Provider account registered. Use Test/Sync for inventory + receipts."
      className={className}
      data-testid="provider-create-form"
    >
      {children}
    </ActionForm>
  );
}

export function ProviderEasyConnectForm({ children, className }: { children: ReactNode; className?: string }) {
  const [isPending, startTransition] = useTransition();
  const [localError, setLocalError] = useState<string | null>(null);
  const [secretRefId, setSecretRefId] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const refresh = useConsoleRefresh();

  const handleAction = (formData: FormData) => {
    setLocalError(null);
    startTransition(async () => {
      try {
        const result = await callAction(easyConnectProviderAction)(formData);
        formRef.current?.reset();
        setSecretRefId(result.secret_ref_id);
        showToast("success", pasteConnectSuccessCopy(result.secret_ref_id));
        refresh("now");
      } catch (err: unknown) {
        const msg =
          err instanceof Error
            ? err.message
            : "Action failed (check budget, permissions, or provider state). See decision inbox and event feed.";
        setLocalError(msg);
        setSecretRefId(null);
        showToast("error", msg);
      }
    });
  };

  return (
    <form
      ref={formRef}
      action={handleAction}
      className={className}
      data-testid="provider-paste-connect-form"
      aria-busy={isPending}
    >
      {children}
      {isPending && (
        <p className="loading" role="status" aria-live="polite" aria-atomic="true">
          Writing key to the local vault…
        </p>
      )}
      {secretRefId ? (
        <p
          className="provider-notice"
          role="status"
          aria-live="polite"
          data-testid="provider-paste-connect-success"
        >
          {pasteConnectSuccessCopy(secretRefId)}
        </p>
      ) : null}
      {localError && (
        <p className="error-banner" role="alert" aria-atomic="true">
          {localError}
        </p>
      )}
    </form>
  );
}

export function BudgetReserveQueueForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={queueOutboxMessageAction}
      successMessage="Outbox queued with active budget reservation. Delivery worker will process."
      className={className}
      data-testid="budget-reserve-queue-form"
    >
      {children}
    </ActionForm>
  );
}

// Additional for >5 coverage (provider test/sync, self-imp canary/rollout, changeset apply)
export function ProviderTestForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={testProviderAccountAction}
      successMessage="Provider test executed. Status and receipt recorded."
      className={className}
      data-testid="provider-test-form"
    >
      {children}
    </ActionForm>
  );
}

export function ProviderSyncForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={syncProviderResourcesAction}
      successMessage="Provider sync started. Inventory + costs reconciled with receipts."
      className={className}
      data-testid="provider-sync-form"
    >
      {children}
    </ActionForm>
  );
}

export function ChangeSetApplyForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={applyChangeSetAction}
      successMessage="ChangeSet applied via ResourceReceipt. Provider state updated."
      className={className}
      data-testid="changeset-apply-form"
    >
      {children}
    </ActionForm>
  );
}
