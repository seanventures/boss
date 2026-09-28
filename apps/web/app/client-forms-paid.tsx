"use client";

import React, { type ReactNode } from "react";
import { ActionForm } from "./client-forms";
import {
  submitSelfImprovementProposalAction,
  recordSelfImprovementCanaryAction,
  recordSelfImprovementRolloutAction,
} from "./actions-paid";

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
