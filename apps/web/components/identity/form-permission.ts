"use client";

/** The permission of the form a SubmitButton sits in: ActionForm provides it, SubmitButton mirrors it. */
import { createContext } from "react";

import type { Permission } from "../../lib/identity/permissions.gen";

export const FormPermission = createContext<Permission | null>(null);
