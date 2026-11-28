// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fileVoiceTranscript,
  VoiceNote,
  type VoiceAttachInput,
  type VoiceAttachResult,
  type VoiceTranscript,
} from "../app/tasks/voice-note";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DOOR: VoiceTranscript = {
  transcriptId: "tr_door",
  source: "phone",
  body: "The door sticks on the second floor.",
  submittedBy: "ada",
  receiptId: "rcpt_door",
};

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function render(element: React.ReactElement) {
  act(() => root!.render(element));
  return host!;
}

function setValue(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function wordsOf(title: string): number {
  return title.trim().split(/\s+/).filter(Boolean).length;
}

describe("a voice slip on a task", () => {
  it("shows the transcript already filed on the task, with its receipt", () => {
    const view = render(
      <VoiceNote
        taskId="tsk_door"
        taskTitle="Second floor door"
        orgId="org_voice"
        projectId="proj_voice"
        submittedBy="ada"
        transcripts={[DOOR]}
      />,
    );
    expect(view.querySelector(".vn-title")?.textContent).toBe("Second floor door");
    const note = view.querySelector('[data-testid="voice-transcript"]');
    expect(note?.getAttribute("data-transcript-id")).toBe("tr_door");
    expect(note?.querySelector('[data-testid="voice-source"]')?.textContent).toBe("phone");
    expect(note?.querySelector('[data-testid="voice-body"]')?.textContent).toBe(
      "The door sticks on the second floor.",
    );
    expect(note?.querySelector('[data-testid="voice-receipt"]')?.textContent).toBe("Receipt rcpt_door");
    expect(note?.querySelector('[data-testid="voice-rule"]')).not.toBeNull();
    const lead = view.querySelector(".vn-lead");
    expect(lead?.tagName).toBe("P");
    expect(lead?.textContent).toContain("does not place a call");
    expect(lead?.textContent).toContain("speech vendor");
    expect(view.querySelector('a[href^="tel:"]')).toBeNull();
    expect(view.querySelector('input[type="tel"]')).toBeNull();
    expect(view.querySelector('input[type="file"]')).toBeNull();
    const buttons = [...view.querySelectorAll("button")].map((button) => button.textContent ?? "");
    expect(buttons.join(" ")).not.toMatch(/dial|transcribe|record/i);
    for (const element of view.querySelectorAll("[title]")) {
      expect(wordsOf(element.getAttribute("title") ?? "")).toBeLessThanOrEqual(14);
    }
  });

  it("files the pasted words and does not add a second copy of the same transcript id", async () => {
    const seen: VoiceAttachInput[] = [];
    const attach = vi.fn(async (input: VoiceAttachInput): Promise<VoiceAttachResult> => {
      seen.push(input);
      if (seen.length === 1) {
        return {
          created: true,
          transcript: {
            transcriptId: input.transcriptId,
            source: input.source,
            body: input.body,
            submittedBy: input.submittedBy,
            receiptId: "rcpt_hinge",
          },
        };
      }
      return {
        created: false,
        transcript: {
          transcriptId: "tr_hinge",
          source: "browser",
          body: "The hinge is the part that sticks.",
          submittedBy: "ada",
          receiptId: "rcpt_hinge",
        },
      };
    });
    const view = render(
      <VoiceNote
        taskId="tsk_door"
        taskTitle="Second floor door"
        orgId="org_voice"
        projectId="proj_voice"
        submittedBy="ada"
        transcripts={[]}
        onAttach={attach}
      />,
    );
    expect(view.querySelector('[data-testid="voice-empty"]')?.textContent).toBe(
      "Nothing is filed on this task yet.",
    );

    setValue(view.querySelector('[data-testid="voice-id"]') as HTMLInputElement, "tr_hinge");
    setValue(
      view.querySelector('[data-testid="voice-words"]') as HTMLTextAreaElement,
      "The hinge is the part that sticks.",
    );
    const phone = view.querySelector('input[name="source"][value="phone"]') as HTMLInputElement;
    act(() => phone.click());
    await act(async () => {
      view.querySelector("form")!.requestSubmit();
    });

    expect(attach).toHaveBeenCalledTimes(1);
    expect(seen[0]).toEqual({
      transcriptId: "tr_hinge",
      source: "phone",
      body: "The hinge is the part that sticks.",
      submittedBy: "ada",
    });
    expect(view.querySelectorAll('[data-testid="voice-transcript"]')).toHaveLength(1);
    expect(view.querySelector('[data-testid="voice-receipt"]')?.textContent).toBe("Receipt rcpt_hinge");

    setValue(view.querySelector('[data-testid="voice-id"]') as HTMLInputElement, "tr_hinge");
    setValue(
      view.querySelector('[data-testid="voice-words"]') as HTMLTextAreaElement,
      "A later telling that must not become a second note.",
    );
    await act(async () => {
      view.querySelector("form")!.requestSubmit();
    });
    expect(attach).toHaveBeenCalledTimes(2);
    expect(view.querySelectorAll('[data-testid="voice-transcript"]')).toHaveLength(1);
    expect(view.querySelector('[data-testid="voice-body"]')?.textContent).toBe(
      "The hinge is the part that sticks.",
    );
    expect(view.querySelector('[data-testid="voice-replay"]')?.textContent).toContain("Already filed.");
  });

  it("shows a refusal and does not pretend the words were filed", async () => {
    const attach = vi.fn(async (): Promise<VoiceAttachResult> => ({
      error: "That task is not in this project.",
    }));
    const view = render(
      <VoiceNote
        taskId="tsk_missing"
        taskTitle="Missing task"
        orgId="org_voice"
        projectId="proj_voice"
        submittedBy="ada"
        transcripts={[]}
        onAttach={attach}
      />,
    );
    setValue(view.querySelector('[data-testid="voice-id"]') as HTMLInputElement, "tr_missing");
    setValue(view.querySelector('[data-testid="voice-words"]') as HTMLTextAreaElement, "   ");
    await act(async () => {
      view.querySelector("form")!.requestSubmit();
    });
    expect(attach).not.toHaveBeenCalled();
    const emptyError = view.querySelector('[data-testid="voice-error"]');
    expect(emptyError?.tagName).toBe("P");
    expect(emptyError?.textContent).toContain("speech vendor");
    expect(view.querySelector('[data-testid="voice-transcript"]')).toBeNull();

    setValue(view.querySelector('[data-testid="voice-words"]') as HTMLTextAreaElement, "The hinge sticks.");
    await act(async () => {
      view.querySelector("form")!.requestSubmit();
    });
    expect(view.querySelector('[data-testid="voice-error"]')?.textContent).toBe(
      "That task is not in this project.",
    );
    expect(view.querySelector('[data-testid="voice-transcript"]')).toBeNull();
  });

  it("does not show a transcript that was already filed on another task", async () => {
    const attach = vi.fn(async (): Promise<VoiceAttachResult> => ({
      created: false,
      transcript: { ...DOOR, taskId: "tsk_other" },
    }));
    const view = render(
      <VoiceNote
        taskId="tsk_door"
        taskTitle="Second floor door"
        orgId="org_voice"
        projectId="proj_voice"
        submittedBy="ada"
        transcripts={[]}
        onAttach={attach}
      />,
    );
    setValue(view.querySelector('[data-testid="voice-id"]') as HTMLInputElement, "tr_door");
    setValue(
      view.querySelector('[data-testid="voice-words"]') as HTMLTextAreaElement,
      "The door sticks on the second floor.",
    );
    await act(async () => {
      view.querySelector("form")!.requestSubmit();
    });
    expect(view.querySelector('[data-testid="voice-transcript"]')).toBeNull();
    expect(view.querySelector('[data-testid="voice-replay"]')?.textContent).toContain("Already filed.");
  });
});

describe("the voice request", () => {
  it("posts the transcript text and no phone number or audio", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      created: true,
      transcript_id: "tr_door",
      source: "phone",
      body: "The door sticks on the second floor.",
      submitted_by: "ada",
      receipt_id: "rcpt_door",
    }), { status: 200, headers: { "content-type": "application/json" } }));

    const result = await fileVoiceTranscript("tsk door", {
      orgId: "org_voice",
      projectId: "proj_voice",
      transcriptId: "tr_door",
      source: "phone",
      body: "The door sticks on the second floor.",
      submittedBy: "ada",
    }, fetchImpl as unknown as typeof fetch);

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/v1/tasks/tsk%20door/voice");
    expect(init.method).toBe("POST");
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({
      org_id: "org_voice",
      project_id: "proj_voice",
      transcript_id: "tr_door",
      source: "phone",
      body: "The door sticks on the second floor.",
      submitted_by: "ada",
    });
    expect(body).not.toHaveProperty("phone_number");
    expect(body).not.toHaveProperty("audio");
    expect(result).toEqual({
      created: true,
      transcript: DOOR,
    });
  });

  it("returns the server's refusal instead of a filed transcript", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      detail: { code: "transcript_empty", message: "Paste the transcript. BOSS does not call a speech vendor." },
    }), { status: 422, headers: { "content-type": "application/json" } }));
    const result = await fileVoiceTranscript("tsk_door", {
      orgId: "org_voice",
      projectId: "proj_voice",
      transcriptId: "tr_empty",
      source: "browser",
      body: "x",
      submittedBy: "ada",
    }, fetchImpl as unknown as typeof fetch);
    expect(result).toEqual({
      error: "Paste the transcript. BOSS does not call a speech vendor.",
    });
  });
});
