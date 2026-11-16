// @vitest-environment happy-dom
/**
 * Login, register, and password reset as pages inside the built app.
 * An edit in the preview changes that app's copy. It does not rewrite the
 * BOSS console sign-in under app/(auth).
 */
import React, { act } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { BuiltAppAuthPreview, type BuiltAppAuthCopy } from "../app/builder/app-login";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SIGN_IN = resolve(process.cwd(), "app/(auth)/login/sign-in.tsx");
const RESET = resolve(process.cwd(), "app/(auth)/reset/reset.tsx");
const SIGN_UP = resolve(process.cwd(), "app/(auth)/signup/signup.tsx");

const LOGIN_LEDE = "Use the email and password this app already knows.";
const REGISTER_LEDE = "This account belongs to the app, not to BOSS.";
const RESET_LEDE = "Ask this app for a reset. It does not sign you in.";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function render(node: React.ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}

function page(kind: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-testid="built-app-page-${kind}"]`);
  if (!el) throw new Error(`missing page ${kind}`);
  return el;
}

function setControl(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function readConsole() {
  return {
    signIn: readFileSync(SIGN_IN, "utf8"),
    reset: readFileSync(RESET, "utf8"),
    signUp: readFileSync(SIGN_UP, "utf8"),
  };
}

describe("built-app login, register, and password reset", () => {
  it("renders the three pages, with long lines in paragraphs", () => {
    render(<BuiltAppAuthPreview appName="Harbor" />);
    const rows: [string, string, string, string][] = [
      ["login", "Sign in", LOGIN_LEDE, "/login"],
      ["register", "Create an account", REGISTER_LEDE, "/register"],
      ["password-reset", "Reset password", RESET_LEDE, "/password-reset"],
    ];
    for (const [kind, headline, lede, path] of rows) {
      const el = page(kind);
      expect(el.getAttribute("data-owner")).toBe("built-app");
      expect(el.getAttribute("data-path")).toBe(path);
      expect(el.querySelector("h1")!.textContent).toBe(headline);
      const prose = el.querySelector('[data-testid="built-app-lede"]')!;
      expect(prose.tagName).toBe("P");
      expect(prose.textContent).toBe(lede);
      expect(lede.split(/\s+/).length).toBeGreaterThan(6);
      expect(el.querySelector("form")!.getAttribute("action")).toBe(path);
    }
    expect(page("login").hidden).toBe(false);
    expect(page("register").hidden).toBe(true);
    expect(page("password-reset").hidden).toBe(true);
    expect(page("login").querySelector('input[type="password"]')).not.toBeNull();
    expect(page("register").querySelector('input[autocomplete="new-password"]')).not.toBeNull();
    expect(page("password-reset").querySelector('input[type="password"]')).toBeNull();
    expect(document.body.textContent).not.toContain("Welcome back");
    expect(document.body.textContent!.toLowerCase()).not.toContain("magic");

    act(() => {
      (document.querySelector('[data-testid="built-app-tab-register"]') as HTMLButtonElement).click();
    });
    expect(page("register").hidden).toBe(false);
    expect(page("login").hidden).toBe(true);
    act(() => {
      (document.querySelector('[data-testid="built-app-tab-password-reset"]') as HTMLButtonElement).click();
    });
    expect(page("password-reset").hidden).toBe(false);
    expect(page("password-reset").textContent).toContain("Harbor");
  });

  it("an edit from the preview changes the built-app copy, not the console sign-in", () => {
    const before = readConsole();
    expect(before.signIn).toContain("Welcome back");
    expect(before.signIn).not.toContain("Members only");
    expect(before.reset).toContain("Forgot your password?");
    const seen: BuiltAppAuthCopy[] = [];
    render(<BuiltAppAuthPreview appName="Harbor" onChange={(next) => seen.push(next)} />);

    const edit = document.querySelector<HTMLButtonElement>('[data-testid="built-app-edit"]')!;
    const tip = edit.getAttribute("title") ?? "";
    expect(tip.split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(14);

    act(() => edit.click());
    setControl(
      document.querySelector<HTMLInputElement>('[data-testid="built-app-edit-headline"]')!,
      "Members only",
    );
    act(() => {
      (document.querySelector('[data-testid="built-app-edit-save"]') as HTMLButtonElement).click();
    });

    expect(page("login").querySelector("h1")!.textContent).toBe("Members only");
    expect(page("login").querySelector('[data-testid="built-app-lede"]')!.textContent).toBe(LOGIN_LEDE);
    expect(page("register").querySelector("h1")!.textContent).toBe("Create an account");
    expect(page("password-reset").querySelector("h1")!.textContent).toBe("Reset password");
    expect(seen.at(-1)!.login.headline).toBe("Members only");
    expect(seen.at(-1)!.register.headline).toBe("Create an account");
    expect(document.body.textContent).not.toContain("Welcome back");

    act(() => {
      (document.querySelector('[data-testid="built-app-tab-register"]') as HTMLButtonElement).click();
    });
    act(() => document.querySelector<HTMLButtonElement>('[data-testid="built-app-edit"]')!.click());
    setControl(
      document.querySelector<HTMLInputElement>('[data-testid="built-app-edit-headline"]')!,
      "Join Harbor",
    );
    act(() => {
      (document.querySelector('[data-testid="built-app-edit-save"]') as HTMLButtonElement).click();
    });
    expect(page("register").querySelector("h1")!.textContent).toBe("Join Harbor");
    expect(page("login").querySelector("h1")!.textContent).toBe("Members only");

    const after = readConsole();
    expect(after.signIn).toBe(before.signIn);
    expect(after.reset).toBe(before.reset);
    expect(after.signUp).toBe(before.signUp);
    expect(after.signIn).toContain("Welcome back");
    expect(after.signIn).not.toContain("Members only");
    expect(after.signIn).not.toContain("Join Harbor");
  });

  it("a blank edit is refused and the built-app headline stays", () => {
    render(<BuiltAppAuthPreview appName="Harbor" />);
    act(() => document.querySelector<HTMLButtonElement>('[data-testid="built-app-edit"]')!.click());
    setControl(
      document.querySelector<HTMLInputElement>('[data-testid="built-app-edit-headline"]')!,
      "   ",
    );
    act(() => {
      (document.querySelector('[data-testid="built-app-edit-save"]') as HTMLButtonElement).click();
    });
    expect(page("login").querySelector("h1")!.textContent).toBe("Sign in");
    const err = document.querySelector('[data-testid="built-app-edit-error"]')!;
    expect(err.tagName).toBe("P");
    expect(err.textContent).toBe("Write something to save.");
  });

  it("a preview edit cannot inject markup into the built app", () => {
    render(<BuiltAppAuthPreview appName="Harbor" />);
    act(() => document.querySelector<HTMLButtonElement>('[data-testid="built-app-edit"]')!.click());
    setControl(
      document.querySelector<HTMLInputElement>('[data-testid="built-app-edit-headline"]')!,
      "<script>alert(1)</script>",
    );
    act(() => {
      (document.querySelector('[data-testid="built-app-edit-save"]') as HTMLButtonElement).click();
    });
    const heading = page("login").querySelector("h1")!;
    expect(heading.textContent).toBe("<script>alert(1)</script>");
    expect(page("login").querySelector("script")).toBeNull();
    expect(heading.querySelector("script")).toBeNull();
  });

  it("submitting the preview does not send a console sign-in", () => {
    const before = readConsole();
    render(<BuiltAppAuthPreview appName="Harbor" />);
    const form = page("login").querySelector("form")!;
    act(() => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    const note = document.querySelector('[data-testid="built-app-preview-note"]')!;
    expect(note.tagName).toBe("P");
    expect(note.textContent).toBe("Preview only. Nothing was sent.");
    expect(readConsole().signIn).toBe(before.signIn);
    expect(document.body.textContent).not.toContain("Welcome back");
  });
});
