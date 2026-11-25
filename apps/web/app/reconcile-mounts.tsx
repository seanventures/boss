"use client";

/**
 * Screens the reconciler wires in from finished slices. Existing areas stay.
 * A press shows that screen under the area already on the page.
 */
import React from "react";
import { LiveFrame } from "./builder/live-frame";
import { ElementSelect } from "./builder/element-select";
import { PublishAudience } from "./builder/publish-audience";
import { BuilderChatPlan } from "./builder/chat-plan";
import { NamedDraft } from "./builder/named-draft";
import { TurnMeter } from "./builder/turn-meter";
import { PreviewShare } from "./builder/preview-share";
import { ApplyBoard } from "./builder/apply-board";
import { ProjectSuite } from "./builder/project-suite";
import { DesignSystemScreen } from "./design-system/screen";
import { ResidencyPin } from "./settings/residency-pin";
import { CveSbomScreen } from "./security/cve-sbom";
import { TenantHandoff } from "./builder/tenant-handoff";
import { PlaygroundScreen } from "./playground/screen";
import { ModelCompare } from "./models/compare";
import { PromptPromote } from "./prompts/promote";
import { ProviderTermsScreen } from "./provider-terms/screen";
import { AppDataScreen } from "./app-data/screen";
import { BuiltAppAuthPreview } from "./builder/app-login";
import { FieldAcl } from "./builder/field-acl";
import { FreezePaths } from "./builder/freeze-paths";
import { LiveRules } from "./builder/live-rules";

// No "Preview frame" (1260, builder/emulator-frame): it painted an iOS/Android phone preview and
// a store upload, and no BOSS image carries an Android SDK, an emulator or Xcode (2026-11-18).
export const MOUNTED_NAV: { id: string; label: string }[] = [
  { id: "1241", label: "Live frame" },
  { id: "1242", label: "Proof marks" },
  { id: "1243", label: "Visitor plate" },
  { id: "1244", label: "Builder thread" },
  { id: "1245", label: "Named draft" },
  { id: "1246", label: "Turn meter" },
  { id: "1251", label: "Preview share" },
  { id: "1252", label: "Apply board" },
  { id: "1253", label: "Project suite" },
  { id: "1254", label: "Design system" },
  { id: "1255", label: "Residency" },
  { id: "1257", label: "CVE & SBOM" },
  { id: "1259", label: "Tenant handoff" },
  { id: "1261", label: "Playground" },
  { id: "1262", label: "Compare" },
  { id: "1263", label: "Prompts" },
  { id: "1264", label: "Provider terms" },
  { id: "1266", label: "Data" },
  { id: "1267", label: "App login" },
  { id: "1268", label: "Field gates" },
  { id: "1269", label: "Freeze paths" },
  { id: "1270", label: "Live rules" },
];

export function MountedSlice({
  id, projectId, actor,
}: {
  id: string;
  projectId: string;
  actor: string;
}) {
  const pid = projectId || "project";
  switch (id) {
    case "1241":
      return <LiveFrame />;
    case "1242":
      return <ElementSelect projectId={pid} actor={actor} />;
    case "1243":
      return (
        <PublishAudience
          preview={{ id: "preview", url: "https://example.com", title: "Preview" }}
          plate={null}
          error={null}
          onPublish={() => {}}
        />
      );
    case "1244":
      return <BuilderChatPlan projectId={pid} />;
    case "1245":
      return <NamedDraft />;
    case "1246":
      return <TurnMeter projectId={pid} turnId="turn" />;
    case "1251":
      return <PreviewShare projectId={pid} actor={actor} />;
    case "1252":
      return <ApplyBoard projectId={pid} actor={actor} />;
    case "1253":
      return <ProjectSuite projectId={pid} />;
    case "1254":
      return <DesignSystemScreen />;
    case "1255":
      return <ResidencyPin projectId={pid} actor={actor} controlPlaneHost="plane.example.com" />;
    case "1257":
      return <CveSbomScreen projectId={pid} actor={actor} />;
    case "1259":
      return <TenantHandoff publish={{ app: "app", imageReference: "app:local" }} />;
    case "1261":
      return <PlaygroundScreen projectId={pid} actor={actor} />;
    case "1262":
      return <ModelCompare projectId={pid} actor={actor} />;
    case "1263":
      return <PromptPromote projectId={pid} actor={actor} />;
    case "1264":
      return <ProviderTermsScreen projectId={pid} />;
    case "1266":
      return <AppDataScreen projectId={pid} />;
    case "1267":
      return <BuiltAppAuthPreview />;
    case "1268":
      return <FieldAcl projectId={pid} actor={actor} />;
    case "1269":
      return <FreezePaths projectId={pid} />;
    case "1270":
      return <LiveRules projectId={pid} actor={actor} />;
    default:
      return null;
  }
}
