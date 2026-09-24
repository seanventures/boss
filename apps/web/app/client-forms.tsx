'use client';

import React, { useEffect, useRef, useState, useTransition, ReactNode } from 'react';
import { useRouter } from 'next/navigation';

import { pasteConnectSuccessCopy } from '../lib/paste-connect';
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
import {
  submitSelfImprovementProposalAction,
  recordSelfImprovementCanaryAction,
  recordSelfImprovementRolloutAction,
} from './actions-paid';

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

interface ActionFormProps {
  action: (formData: FormData) => Promise<void>;
  successMessage: string;
  className?: string;
  children: ReactNode;
  'data-testid'?: string;
}

export function ActionForm({
  action,
  successMessage,
  className,
  children,
  'data-testid': testId,
}: ActionFormProps): React.ReactElement {
  const [isPending, startTransition] = useTransition();
  const [localError, setLocalError] = useState<string | null>(null);
  const router = useRouter();

  const handleAction = (formData: FormData) => {
    setLocalError(null);
    startTransition(async () => {
      try {
        await action(formData);
        // Success: toast + router.refresh() to pick up the revalidatePath done inside the server action.
        // This makes the updated lists (KPI registry, budget, feeds, events) visible immediately after client submit.
        showToast('success', successMessage);
        router.refresh();
      } catch (err: unknown) {
        const msg =
          err instanceof Error
            ? err.message
            : 'Action failed (check budget, permissions, or provider state). See decision inbox and event feed.';
        setLocalError(msg);
        showToast('error', msg);
      }
    });
  };

  return (
    <form
      action={handleAction}
      className={className}
      data-testid={testId}
      aria-busy={isPending}
    >
      {children}
      {isPending && (
        <p className="loading" role="status" aria-live="polite" aria-atomic="true">
          Processing… (validating budget reservation, policy, and lease)
        </p>
      )}
      {localError && (
        <p className="error-banner" role="alert" aria-atomic="true">
          {localError}
        </p>
      )}
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
  const router = useRouter();

  const handleAction = (formData: FormData) => {
    setLocalError(null);
    startTransition(async () => {
      try {
        const result = await easyConnectProviderAction(formData);
        formRef.current?.reset();
        setSecretRefId(result.secret_ref_id);
        showToast("success", pasteConnectSuccessCopy(result.secret_ref_id));
        router.refresh();
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

export function SelfImprovementProposalForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={submitSelfImprovementProposalAction}
      successMessage="Improvement proposal submitted. Task + decision inbox entry created."
      className={className}
      data-testid="self-imp-proposal-form"
    >
      {children}
    </ActionForm>
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

export function SelfImpCanaryForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={recordSelfImprovementCanaryAction}
      successMessage="Canary recorded. Proposal status updated; decision may advance."
      className={className}
      data-testid="self-imp-canary-form"
    >
      {children}
    </ActionForm>
  );
}

export function SelfImpRolloutForm({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <ActionForm
      action={recordSelfImprovementRolloutAction}
      successMessage="Rollout recorded. Changes live (with rollback plan on file)."
      className={className}
      data-testid="self-imp-rollout-form"
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
