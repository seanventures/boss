"use client";

/**
 * The build bench on the default overview (BOSSAPP-1273).
 *
 * Same jobs as a studio home: a status line, a headline, one composer, an add menu,
 * a mode, a microphone when the browser can dictate, "Easy Mode", chips, and
 * a template gallery. Drawn as a job ticket in the console's own type and tokens.
 *
 * Chips only fill the sentence. Easy Mode is the only control that starts a build, and an
 * empty sentence is allowed — the server invents the spec. A refused launch never
 * shows a project or a run.
 */
import {
  Camera,
  CaretDown,
  Cube,
  Flask,
  GameController,
  GithubLogo,
  Globe,
  GoogleDriveLogo,
  Hammer,
  MagicWand,
  MapTrifold,
  Microphone,
  Paperclip,
  Stop,
  UploadSimple,
  VirtualReality,
  type Icon,
} from "@phosphor-icons/react";
import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { askConsent, consentFromBody } from "../lib/identity/consent";
import { openStartSheet } from "./start-project/start-host";

import { TemplatePreview } from "../components/v2/templates/template-previews";

import "./build-home.css";

const PLACEHOLDER = "Describe an app and let BOSS do the rest";

const CHIPS = [
  "Build a web app",
  "Maps",
  "Create a game",
  "Three.js 3D scenes",
  "Build an XR app",
] as const;

/** Each chip's gallery category (web, maps, game, 3d, xr): its icon and its tint. */
const CHIP_KIND: Record<(typeof CHIPS)[number], { kind: string; icon: Icon }> = {
  "Build a web app": { kind: "web", icon: Globe },
  Maps: { kind: "maps", icon: MapTrifold },
  "Create a game": { kind: "game", icon: GameController },
  "Three.js 3D scenes": { kind: "3d", icon: Cube },
  "Build an XR app": { kind: "xr", icon: VirtualReality },
};

const MODES = [
  { id: "build", label: "Build" },
  { id: "prototype", label: "Prototype" },
] as const;

const MODE_ICON: Record<(typeof MODES)[number]["id"], Icon> = { build: Hammer, prototype: Flask };

/** Icons sit beside their words, never instead of them, so every one is decorative. */
const ICON = { size: "1.15em", weight: "bold", "aria-hidden": true, focusable: false } as const;

type Phase = "ready" | "starting" | "started" | "error";

type TemplateCard = {
  id: string;
  title: string;
  category: string;
  blurb: string;
  prompt: string;
  /** Null when this install builds it; else what it needs ("Needs Go") and the command. */
  needs: { label: string; enable: string; message: string } | null;
};

/** `available: false` with `needs` (GET /v1/factory/templates): an opt-in toolchain is off. */
function needsOf(record: Record<string, unknown>): TemplateCard["needs"] {
  if (record.available !== false) return null;
  const needs = record.needs && typeof record.needs === "object" ? (record.needs as Record<string, unknown>) : {};
  const enable = str(needs.enable);
  return {
    label: str(needs.label) || "Not on this install",
    enable,
    message: str(needs.message) || "This install cannot build this template yet.",
  };
}

type Attachment = { id: string; name: string };

type LaunchIds = { projectId: string; runId: string };

type Gallery =
  | { state: "loading" }
  | { state: "ready"; templates: TemplateCard[] }
  | { state: "error"; message: string };

type Rec = {
  lang: string;
  interimResults: boolean;
  onresult: ((ev: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

function recognitionCtor(): (new () => Rec) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: new () => Rec;
    webkitSpeechRecognition?: new () => Rec;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function str(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function first(obj: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = str(obj[key]);
    if (value) return value;
  }
  return "";
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  if (text.trim().startsWith("<")) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { message: text.slice(0, 280) };
  }
}

/** What a refusal's machine code means, in words. A bare code is never shown to a person. */
const CODE_WORDS: Record<string, string> = {
  consent_required: "Not started: the agents need your consent first. Press Easy Mode to give it.",
  personal_data_blocked: "Not started: the description holds a card number or a national id. Take it out and press Easy Mode again.",
  personal_data_ask: "Not started: the description holds an email or a phone number. Take it out and press Easy Mode again.",
  budget_refused: "Not started: the budget refused this build.",
  launch_incomplete: "The build did not start: BOSS did not name a project and a run.",
  project_exists: "Not started: a project with this name already exists.",
  agent_without_passport: "Not started: an agent of this project has no Passport yet.",
  github_not_connected: "Not started: connect GitHub first (Setup › GitHub).",
  drive_not_connected: "Not started: that Drive source is not connected.",
  unknown_template: "Not started: that template is no longer in the gallery.",
  toolchain_not_enabled: "Not started: this template needs a toolchain this BOSS has not turned on.",
  unknown_attachment: "Not started: an attachment is no longer there. Attach it again.",
  needs_model: "No model is connected, so nothing was started or reserved. Add one in Setup › Models, then press Easy Mode again.",
};

/** Where a refusal is fixed, when the API names it (`fix_href`: Setup › Models for no model). */
function fixHrefOf(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const href = str((data as Record<string, unknown>).fix_href);
  return href.startsWith("?") || href.startsWith("/") ? href : null;
}

/** A sentence, not a code: a word with no space in it is looked up, never printed as it is. */
function sentence(value: string): string {
  if (!value) return "";
  if (/\s/.test(value)) return value;
  return CODE_WORDS[value] ?? "";
}

function messageOf(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const record = data as Record<string, unknown>;
  for (const key of ["message", "error", "detail"]) {
    const value = sentence(str(record[key]));
    if (value) return value;
  }
  const code = str(record.error);
  return (code && CODE_WORDS[code]) || fallback;
}

/** The consent sheet was closed: the project is made, idle, and waits for this consent. */
const CONSENT_DECLINED =
  "Not started: the agents need your consent to build. The project is made and waits, idle. Press Easy Mode again to give consent and start it.";

function templatesFrom(data: unknown): TemplateCard[] {
  const list = Array.isArray(data)
    ? data
    : data && typeof data === "object" && Array.isArray((data as { templates?: unknown }).templates)
      ? (data as { templates: unknown[] }).templates
      : null;
  if (!list) throw new Error("Templates did not load.");
  const cards: TemplateCard[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const id = first(record, ["id", "template_id"]);
    const title = first(record, ["title", "name"]);
    if (!id || !title) continue;
    cards.push({
      id,
      title,
      category: first(record, ["category", "kind"]),
      blurb: first(record, ["blurb", "summary", "description"]),
      prompt: first(record, ["prompt"]),
      needs: needsOf(record),
    });
  }
  if (list.length > 0 && cards.length === 0) throw new Error("Templates did not load.");
  return cards;
}

function idsFrom(data: unknown): string[] {
  if (!data || typeof data !== "object") return [];
  const record = data as Record<string, unknown>;
  for (const key of ["attachment_ids", "ids"]) {
    const list = record[key];
    if (!Array.isArray(list)) continue;
    return list.map((item) => str(item)).filter(Boolean);
  }
  const single = first(record, ["id", "attachment_id"]);
  return single ? [single] : [];
}

function launchIds(data: unknown): LaunchIds | null {
  if (!data || typeof data !== "object") return null;
  const record = data as Record<string, unknown>;
  const projectId = first(record, ["project_id", "projectId"]);
  const runId = first(record, ["run_id", "runId"]);
  if (!projectId && !runId) return null;
  return { projectId, runId };
}

function rowsFor(files: File[], ids: string[]): Attachment[] {
  if (ids.length === files.length) {
    return ids.map((id, index) => ({ id, name: files[index]?.name || id }));
  }
  if (ids.length === 1) return [{ id: ids[0]!, name: files.map((file) => file.name).filter(Boolean).join(", ") || ids[0]! }];
  return ids.map((id) => ({ id, name: id }));
}

const STATUS: Record<Phase, string> = {
  ready: "Ready to build",
  starting: "Starting",
  started: "Build started",
  error: "Not started",
};

export function BuildHome() {
  const promptId = useId();
  const menuId = useId();
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const addRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<Rec | null>(null);
  const generation = useRef(0);

  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<(typeof MODES)[number]["id"]>("build");
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [phase, setPhase] = useState<Phase>("ready");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [launch, setLaunch] = useState<LaunchIds | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [launchFix, setLaunchFix] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [driveNote, setDriveNote] = useState(false);
  const [canDictate, setCanDictate] = useState(false);
  const [listening, setListening] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  const [gallery, setGallery] = useState<Gallery>({ state: "loading" });
  const [galleryTicket, setGalleryTicket] = useState(0);

  const touch = () => {
    generation.current += 1;
    setLaunch(null);
    setLaunchError(null);
    setLaunchFix(null);
    setPhase("ready");
  };

  useEffect(() => {
    setCanDictate(recognitionCtor() !== null);
    return () => {
      try { recRef.current?.stop(); } catch { /* already stopped */ }
    };
  }, []);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const onDoc = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      addRef.current?.focus();
    };
    document.addEventListener("click", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  useEffect(() => {
    let live = true;
    setGallery({ state: "loading" });
    fetch("/api/factory/templates")
      .then(async (res) => {
        const data = await readBody(res);
        if (!res.ok) throw new Error(messageOf(data, "Templates did not load."));
        return templatesFrom(data);
      })
      .then((templates) => {
        if (live) setGallery({ state: "ready", templates });
      })
      .catch((error: unknown) => {
        if (!live) return;
        setGallery({ state: "error", message: error instanceof Error && error.message ? error.message : "Templates did not load." });
      });
    return () => { live = false; };
  }, [galleryTicket]);

  const upload = async (files: File[]) => {
    if (!files.length || uploading) return;
    setUploading(true);
    setUploadError(null);
    try {
      const body = new FormData();
      for (const file of files) body.append("file", file);
      const res = await fetch("/api/factory/attachments", { method: "POST", body });
      const data = await readBody(res);
      if (!res.ok) {
        setUploadError(messageOf(data, "The file did not upload."));
        return;
      }
      const ids = idsFrom(data);
      if (!ids.length) {
        setUploadError("The upload did not return an id.");
        return;
      }
      setAttachments((current) => [...current, ...rowsFor(files, ids)]);
      touch();
    } catch (error: unknown) {
      setUploadError(error instanceof Error && error.message ? error.message : "The file did not upload.");
    } finally {
      setUploading(false);
    }
  };

  const onFiles = (input: HTMLInputElement) => {
    const files = [...(input.files ?? [])];
    input.value = "";
    if (files.length) void upload(files);
  };

  const lucky = async () => {
    if (busy || uploading) return;
    const mine = generation.current;
    const body = {
      prompt,
      mode,
      template_id: templateId,
      attachment_ids: [...new Set(attachments.map((item) => item.id))],
    };
    setBusy(true);
    setLaunch(null);
    setLaunchError(null);
    setLaunchFix(null);
    setPhase("starting");
    const send = async () => {
      const res = await fetch("/api/factory/launch", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify(body),
      });
      return { res, data: await readBody(res) };
    };
    try {
      let { res, data } = await send();
      if (generation.current !== mine) return;
      // CONSENT, THE CONSOLE'S ONE WAY (lib/identity/consent.ts): the build start needs the
      // person's grant for the project's agents. The API kept the project; the sheet creates the
      // grant and the same launch is sent again unchanged, so it builds in that same project.
      const asked = res.status === 409 ? consentFromBody(data) : null;
      if (asked) {
        const granted = await askConsent(asked);
        if (generation.current !== mine) return;
        if (!granted) {
          setPhase("error");
          setLaunch(null);
          setLaunchError(CONSENT_DECLINED);
          return;
        }
        ({ res, data } = await send());
        if (generation.current !== mine) return;
      }
      if (!res.ok) {
        setPhase("error");
        setLaunchError(messageOf(data, "The build did not start."));
        setLaunchFix(fixHrefOf(data));
        setLaunch(null);
        return;
      }
      const ids = launchIds(data);
      if (!ids) {
        setPhase("error");
        setLaunch(null);
        setLaunchError("The launch did not name a project or a run.");
        return;
      }
      setLaunch(ids);
      setPhase("started");
    } catch (error: unknown) {
      if (generation.current !== mine) return;
      setPhase("error");
      setLaunch(null);
      setLaunchError(error instanceof Error && error.message ? error.message : "The build did not start.");
    } finally {
      setBusy(false);
    }
  };

  const dictate = () => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    if (listening && recRef.current) {
      try { recRef.current.stop(); } catch { /* already stopped */ }
      setListening(false);
      return;
    }
    setMicError(null);
    const rec = new Ctor();
    rec.interimResults = false;
    rec.onresult = (event) => {
      const parts: string[] = [];
      for (let index = 0; index < event.results.length; index += 1) {
        const text = event.results[index]?.[0]?.transcript?.trim();
        if (text) parts.push(text);
      }
      if (!parts.length) return;
      const said = parts.join(" ");
      setPrompt((current) => (current.trim() ? `${current.trim()} ${said}` : said));
      touch();
    };
    rec.onerror = () => setMicError("The microphone did not start.");
    rec.onend = () => setListening(false);
    recRef.current = rec;
    try {
      rec.start();
      setListening(true);
    } catch {
      setListening(false);
      setMicError("The microphone did not start.");
    }
  };

  const choose = (card: TemplateCard) => {
    if (card.needs) return;
    setTemplateId(card.id);
    if (card.prompt) setPrompt(card.prompt);
    touch();
    promptRef.current?.focus();
  };

  return (
    <div className="bh" data-testid="build-home" data-template-id={templateId ?? ""}>
      <div className="bh-bench">
        <div className="bh-head">
          <p className="bh-status" data-state={phase} data-testid="build-status">
            <span className="bh-dot" aria-hidden="true" />
            {STATUS[phase]}
          </p>
          <h1 className="bh-title">Build with BOSS</h1>
          <p className="bh-lede">Describe the app. BOSS builds it.</p>
        </div>

        <div className="bh-ticket">
          <label className="bh-prompt-wrap" htmlFor={promptId}>
            <span className="bh-sr">Describe an app</span>
            <textarea
              id={promptId}
              ref={promptRef}
              className="bh-prompt"
              data-testid="build-prompt"
              placeholder={PLACEHOLDER}
              value={prompt}
              rows={5}
              autoComplete="off"
              onChange={(event) => {
                setPrompt(event.target.value);
                touch();
              }}
            />
          </label>
          <div className="bh-foot">
            <div className="bh-addwrap" ref={menuRef}>
              <button
                ref={addRef}
                type="button"
                className="bh-add"
                data-testid="build-add"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                aria-controls={menuOpen ? menuId : undefined}
                onClick={() => setMenuOpen((open) => !open)}
              >
                <Paperclip {...ICON} data-testid="build-add-icon" />
                Add
              </button>
              {menuOpen ? (
                <ul id={menuId} className="bh-menu" role="menu" data-testid="build-add-menu">
                  <li role="none">
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="build-add-github"
                      onClick={() => {
                        setMenuOpen(false);
                        addRef.current?.focus();
                        openStartSheet("import");
                      }}
                    >
                      <span className="bh-menu-row"><GithubLogo {...ICON} />Import from GitHub</span>
                    </button>
                  </li>
                  <li role="none">
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="build-add-drive"
                      onClick={() => {
                        setDriveNote(true);
                        setMenuOpen(false);
                        addRef.current?.focus();
                      }}
                    >
                      <span className="bh-menu-row"><GoogleDriveLogo {...ICON} />Drive</span>
                      <span className="bh-menu-note">Drive is not connected</span>
                    </button>
                  </li>
                  <li role="none">
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="build-add-upload"
                      onClick={() => {
                        setMenuOpen(false);
                        uploadRef.current?.click();
                      }}
                    >
                      <span className="bh-menu-row"><UploadSimple {...ICON} />Upload files</span>
                    </button>
                  </li>
                  <li role="none">
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="build-add-camera"
                      onClick={() => {
                        setMenuOpen(false);
                        cameraRef.current?.click();
                      }}
                    >
                      <span className="bh-menu-row"><Camera {...ICON} />Camera</span>
                    </button>
                  </li>
                </ul>
              ) : null}
            </div>
            <label className="bh-mode" data-mode={mode}>
              <span>Mode</span>
              <span className="bh-mode-pill">
                {React.createElement(MODE_ICON[mode], { ...ICON, className: "bh-mode-icon", "data-testid": "build-mode-icon" })}
                <select
                  aria-label="Mode"
                  data-testid="build-mode"
                  value={mode}
                  onChange={(event) => {
                    const next = event.target.value;
                    if (next === "build" || next === "prototype") setMode(next);
                    touch();
                  }}
                >
                  {MODES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>
                <CaretDown {...ICON} className="bh-mode-caret" />
              </span>
            </label>
            {canDictate ? (
              <button
                type="button"
                className="bh-mic"
                data-testid="build-mic"
                aria-pressed={listening}
                aria-label="Dictate"
                onClick={dictate}
              >
                {listening ? <Stop {...ICON} weight="fill" data-testid="build-mic-icon" /> : <Microphone {...ICON} data-testid="build-mic-icon" />}
                {listening ? "Stop" : "Mic"}
              </button>
            ) : null}
            <button
              type="button"
              className="bh-lucky"
              data-testid="build-lucky"
              disabled={busy || uploading}
              aria-busy={busy || uploading}
              onClick={() => void lucky()}
            >
              <MagicWand {...ICON} className="bh-lucky-icon" data-testid="build-lucky-icon" />
              Easy Mode
            </button>
          </div>
          <input
            ref={uploadRef}
            className="bh-sr"
            type="file"
            multiple
            tabIndex={-1}
            aria-label="Upload files"
            data-testid="build-upload-input"
            onChange={(event) => onFiles(event.currentTarget)}
          />
          <input
            ref={cameraRef}
            className="bh-sr"
            type="file"
            accept="image/*"
            capture="environment"
            tabIndex={-1}
            aria-label="Camera"
            data-testid="build-camera-input"
            onChange={(event) => onFiles(event.currentTarget)}
          />
        </div>

        <ul className="bh-chips" aria-label="Suggestions">
          {CHIPS.map((chip) => {
            const { kind, icon: ChipIcon } = CHIP_KIND[chip];
            return (
              <li key={chip}>
                <button
                  type="button"
                  className="bh-chip"
                  data-testid="build-chip"
                  data-kind={kind}
                  onClick={() => {
                    setPrompt(chip);
                    touch();
                    promptRef.current?.focus();
                  }}
                >
                  <ChipIcon {...ICON} className="bh-chip-icon" />
                  {chip}
                </button>
              </li>
            );
          })}
        </ul>

        {launch ? (
          <p className="bh-ids" role="status" data-testid="build-launch">
            {launch.projectId ? <>Project <span data-testid="build-project-id">{launch.projectId}</span></> : null}
            {launch.projectId && launch.runId ? <span aria-hidden="true"> · </span> : null}
            {launch.runId ? <>Run <span data-testid="build-run-id">{launch.runId}</span></> : null}
          </p>
        ) : null}
        {launchError ? (
          <p className="bh-error" role="alert" data-testid="build-launch-error">
            {launchError}
            {launchFix ? <> <a href={launchFix} data-testid="build-launch-fix">{/models/.test(launchFix) ? "Add a model" : "Fix it"}</a></> : null}
          </p>
        ) : null}
        {driveNote ? <p className="bh-note" role="status" data-testid="build-drive-note">Drive is not connected</p> : null}
        {micError ? <p className="bh-error" role="alert">{micError}</p> : null}

        {attachments.length ? (
          <ul className="bh-files" aria-label="Attached files">
            {attachments.map((item) => (
              <li key={`${item.id}:${item.name}`} data-testid="build-attachment" data-attachment-id={item.id}>
                <span>{item.name}</span>
                <code>{item.id}</code>
              </li>
            ))}
          </ul>
        ) : null}
        {uploading ? <p className="bh-note" role="status">Uploading</p> : null}
        {uploadError ? <p className="bh-error" role="alert" data-testid="build-upload-error">{uploadError}</p> : null}
      </div>

      <section className="bh-gallery" aria-label="Templates" data-testid="build-templates">
        <div className="bh-gallery-head">
          <h2>Templates</h2>
        </div>
        {gallery.state === "loading" ? <p className="bh-note" role="status" data-testid="build-templates-loading">Loading templates</p> : null}
        {gallery.state === "error" ? (
          <div className="bh-gallery-error" role="alert" data-testid="build-templates-error">
            <p>{gallery.message}</p>
            <button type="button" onClick={() => setGalleryTicket((n) => n + 1)}>Try again</button>
          </div>
        ) : null}
        {gallery.state === "ready" && gallery.templates.length === 0 ? (
          <p className="bh-note" data-testid="build-templates-empty">No templates yet. The bench still builds from the sentence.</p>
        ) : null}
        {gallery.state === "ready" && gallery.templates.length > 0 ? (
          <TemplateCards templates={gallery.templates} selected={templateId} onChoose={choose} />
        ) : null}
      </section>
    </div>
  );
}

/** Two rows of cards by default; the rest behind "Show all". Rows are counted from the grid's
 *  own resolved columns, so two rows stay two rows at every width. Without a layout (a test DOM)
 *  it counts three columns. */
export const COLLAPSED_ROWS = 2;
const FALLBACK_COLUMNS = 3;

function useGridColumns(ref: React.RefObject<HTMLElement | null>): number {
  const [cols, setCols] = useState(FALLBACK_COLUMNS);
  // Measured before paint, so the first frame already shows two rows at this width (no 6→8 jump).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof window === "undefined") return;
    const read = () => {
      const tracks = window.getComputedStyle(el).gridTemplateColumns || "";
      // Resolved tracks read "224px 224px …"; anything else (no layout) keeps the fallback.
      const n = /\dpx/.test(tracks) ? tracks.trim().split(/\s+/).length : 0;
      if (n > 0) setCols((prev) => (prev === n ? prev : n));
    };
    read();
    // The list's own size covers a window resize; the window listener is only for browsers without it.
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(read);
      ro.observe(el);
      return () => ro.disconnect();
    }
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, [ref]);
  return cols;
}

/** The server's reason, with its `command` spans shown as code. */
function withCode(text: string): React.ReactNode {
  return text.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}

/** Two rows' worth, and never without the selected card: a pick past the fold takes the last
 *  slot, so the highlighted choice stays on screen after "Show fewer" or a narrower window. */
export function foldedCards<T extends { id: string }>(cards: T[], limit: number, selected: string | null): T[] {
  const head = cards.slice(0, limit);
  if (!selected || head.some((card) => card.id === selected)) return head;
  const pick = cards.find((card) => card.id === selected);
  return pick ? [...head.slice(0, Math.max(0, limit - 1)), pick] : head;
}

function TemplateCards({ templates, selected, onChoose }: {
  templates: TemplateCard[];
  selected: string | null;
  onChoose: (card: TemplateCard) => void;
}) {
  const listRef = useRef<HTMLUListElement>(null);
  const cols = useGridColumns(listRef);
  const [all, setAll] = useState(false);
  const listId = useId();
  const limit = Math.max(1, cols) * COLLAPSED_ROWS;
  const folds = templates.length > limit;
  const shown = all || !folds ? templates : foldedCards(templates, limit, selected);
  return (
    <>
      <ul className="bh-cards" id={listId} ref={listRef} data-testid="build-template-list"
          data-columns={cols} data-expanded={all || !folds ? "true" : "false"}>
        {shown.map((card) => {
          const whyId = `${listId}-${card.id}-needs`;
          const showCommand = !!card.needs?.enable && !card.needs.message.includes(card.needs.enable);
          return (
            <li key={card.id}>
              <button
                type="button"
                className="bh-card"
                data-testid="build-template"
                data-template-id={card.id}
                aria-pressed={card.needs ? undefined : selected === card.id}
                aria-disabled={card.needs ? "true" : undefined}
                aria-describedby={card.needs ? whyId : undefined}
                data-unavailable={card.needs ? "true" : undefined}
                onClick={() => onChoose(card)}
              >
                <span className="bh-card-art">
                  <TemplatePreview id={card.id} category={card.category} />
                </span>
                <span className="bh-card-text">
                  {card.category ? <span className="bh-cat">{card.category}</span> : null}
                  <span className="bh-card-title">{card.title}</span>
                  {card.blurb ? <span className="bh-blurb">{card.blurb}</span> : null}
                  {card.needs ? (
                    <span className="bh-needs" id={whyId} data-testid="build-template-needs">
                      <span className="bh-needs-label">{card.needs.label}</span>
                      <span className="bh-needs-why">{withCode(card.needs.message)}</span>
                      {showCommand ? <span className="bh-needs-how">Turn on: <code>{card.needs.enable}</code></span> : null}
                    </span>
                  ) : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {folds ? (
        <button type="button" className="bh-more" data-testid="build-templates-more"
                aria-expanded={all} aria-controls={listId} onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${templates.length} templates`}
        </button>
      ) : null}
    </>
  );
}
