/**
 * The Workflow Studio parts the Workflows area (Runs, Library) mounts, in one place.
 *
 * The editor, the lane and the API client are built by other streams against the contract in
 * lib/workflow-editor-types.ts. The area imports them from HERE only, so wiring the real
 * modules in is a change to this file, and a test that wants a stand-in mocks one path.
 */
export {
  DefineStepModal, FlowLane, MiniLane, NameInUseError, WorkflowEditor, deleteWorkflow, fetchLibrary,
  fetchSpec, renameWorkflow,
} from "../../lib/workflow-editor-types";
export type { CustomStepDef, Spec, WorkflowSummary } from "../../lib/workflow-editor-types";
