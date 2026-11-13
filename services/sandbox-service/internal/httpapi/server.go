// Package httpapi serves the MicroVMProvider HTTP contract.
// Without a configured backend it stays the fail-closed scaffold; with one
// (process or firecracker) admitted submits run for real via the runner.
package httpapi

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
	"github.com/seanheiney/boss/services/sandbox-service/internal/run"
)

type Server struct {
	Store  *job.Store
	Token  string
	Runner *run.Runner // nil = scaffold mode (no execution backend)
	mux    *http.ServeMux
}

func New(store *job.Store, token string) *Server {
	s := &Server{Store: store, Token: token, mux: http.NewServeMux()}
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/v1/sandbox/health", s.handleHealth)
	s.mux.HandleFunc("/v1/sandbox/helpers", s.handleHelpers)
	s.mux.HandleFunc("/v1/sandbox/tasks", s.handleTasks)
	s.mux.HandleFunc("/v1/sandbox/tasks/", s.handleTask)
	return s
}

// NewWithRunner wires a live execution backend behind the same contract.
func NewWithRunner(store *job.Store, token string, runner *run.Runner) *Server {
	s := New(store, token)
	s.Runner = runner
	return s
}

func (s *Server) Handler() http.Handler { return s }

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *Server) authorized(r *http.Request) bool {
	if s.Token == "" {
		return true
	}
	if r.Header.Get("X-Sandbox-Token") == s.Token {
		return true
	}
	// The same shared secret as a standard Bearer credential.
	return r.Header.Get("Authorization") == "Bearer "+s.Token
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	frozen := s.Store.ClaimsFrozen()
	backendLive := s.Store.BackendLive()
	fcLive := s.Store.FirecrackerLive()
	reason := ""
	switch {
	case frozen:
		reason = contract.ReasonClaimsFrozen
	case s.Runner == nil || s.Store.BackendName() == "none":
		reason = contract.ReasonFirecrackerDisabled
	case !backendLive:
		reason = s.Store.BackendError() // names the missing piece
	}
	// ok is true only when a configured backend is actually ready AND claims
	// are not frozen. The backend-less scaffold never sets this; health never
	// invents host.connected=true.
	ready := s.Runner != nil && backendLive && !frozen
	capacity := 0
	if ready {
		capacity = 1 // serialized per-task capacity; pooling is a later tier
	}
	body := map[string]any{
		"ok":                ready,
		"service":           "up",
		"backend":           s.Store.BackendName(),
		"firecracker_live":  fcLive,
		"execution_ready":   ready,
		"execution_claimed": ready,
		"claims_frozen":     frozen,
		"reason":            reason,
		"contract_version":  contract.Version,
		"capacity":          capacity,
		"available":         capacity,
		"running":           s.Store.RunningCount(),
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		writeErr(w, http.StatusUnauthorized, contract.ReasonUnauthorized, "X-Sandbox-Token rejected")
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, contract.ReasonContractInvalid, "unreadable body")
		return
	}
	req, err := contract.DecodeSubmit(raw)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, contract.ReasonContractInvalid, err.Error())
		return
	}
	reason, message := s.Store.Admit(req)
	if reason != "" {
		writeErr(w, http.StatusUnprocessableEntity, reason, message)
		return
	}
	// A live backend runs everything admitted (echo included) for real.
	if s.Runner != nil && s.Store.BackendLive() {
		created, err := s.Runner.Submit(req)
		if err != nil {
			writeErr(w, http.StatusUnprocessableEntity, contract.ReasonBackendUnavailable, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, contract.SubmitResponse{
			SandboxTaskID: created.ID,
			Status:        contract.StatusQueued,
		})
		return
	}
	if !contract.IsEcho(req) {
		writeErr(w, http.StatusUnprocessableEntity, contract.ReasonFirecrackerDisabled,
			"Firecracker backend is not enabled; this in-repo S1 scaffold does not run live microVMs")
		return
	}
	created := s.Store.CreateEcho(req)
	writeJSON(w, http.StatusAccepted, contract.SubmitResponse{
		SandboxTaskID: created.ID,
		Status:        contract.StatusQueued,
	})
}

func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		writeErr(w, http.StatusUnauthorized, contract.ReasonUnauthorized, "X-Sandbox-Token rejected")
		return
	}
	rest := strings.TrimPrefix(r.URL.Path, "/v1/sandbox/tasks/")
	rest = strings.Trim(rest, "/")
	if rest == "" {
		http.NotFound(w, r)
		return
	}
	parts := strings.Split(rest, "/")
	id := parts[0]
	verb := ""
	if len(parts) > 1 {
		verb = parts[1]
	}

	switch {
	case verb == "" && r.Method == http.MethodGet:
		s.poll(w, id)
	case verb == "" && r.Method == http.MethodDelete:
		s.release(w, id)
	case verb == "heartbeat" && r.Method == http.MethodPost:
		s.heartbeat(w, id)
	case verb == "hold" && r.Method == http.MethodPost:
		s.hold(w, r, id)
	case verb == "cancel" && r.Method == http.MethodPost:
		s.cancel(w, r, id)
	case verb == "fs" && r.Method == http.MethodGet:
		s.fs(w, r, id)
	case verb == "logs" && r.Method == http.MethodGet:
		s.logs(w, id)
	case verb == "preview" && r.Method == http.MethodPost:
		s.preview(w)
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) poll(w http.ResponseWriter, id string) {
	job, ok := s.Store.Get(id)
	if !ok {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	writeJSON(w, http.StatusOK, s.statusOf(job))
}

func (s *Server) cancel(w http.ResponseWriter, r *http.Request, id string) {
	var body struct {
		Reason string `json:"reason"`
	}
	_ = json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&body)
	if s.Runner != nil {
		if !s.Runner.Cancel(id, body.Reason) {
			writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
			return
		}
	} else {
		if _, ok := s.Store.Get(id); !ok {
			writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
			return
		}
		s.Store.MarkCancelled(id, body.Reason)
	}
	job, _ := s.Store.Get(id)
	writeJSON(w, http.StatusOK, s.statusOf(job))
}

// fs serves the retained workspace for root-causing: the guest's /work/... maps
// onto <TaskDir>/work/... on the host. Directories list entries; files return
// content (bounded).
func (s *Server) fs(w http.ResponseWriter, r *http.Request, id string) {
	job, ok := s.Store.Get(id)
	if !ok {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	if job.TaskDir == "" {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing,
			"this sandbox has no retained workspace (echo stub)")
		return
	}
	guestPath := strings.TrimSpace(r.URL.Query().Get("path"))
	if guestPath == "" {
		guestPath = "/work/repo"
	}
	rel := strings.TrimPrefix(path.Clean("/"+strings.TrimPrefix(guestPath, "/")), "/")
	if rel != "work" && !strings.HasPrefix(rel, "work/") {
		writeErr(w, http.StatusUnprocessableEntity, contract.ReasonContractInvalid,
			"only paths under /work are browsable")
		return
	}
	hostPath := filepath.Join(job.TaskDir, filepath.FromSlash(rel))
	info, err := os.Stat(hostPath)
	if err != nil {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "path not found in retained workspace")
		return
	}
	if info.IsDir() {
		entries, err := os.ReadDir(hostPath)
		if err != nil {
			writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "unreadable directory")
			return
		}
		out := make([]map[string]any, 0, len(entries))
		for _, e := range entries {
			out = append(out, map[string]any{"name": e.Name(), "dir": e.IsDir()})
		}
		writeJSON(w, http.StatusOK, map[string]any{"path": guestPath, "entries": out})
		return
	}
	const maxFile = 256 << 10
	raw, err := os.ReadFile(hostPath)
	if err != nil {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "unreadable file")
		return
	}
	truncated := false
	if len(raw) > maxFile {
		raw = raw[:maxFile]
		truncated = true
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"path": guestPath, "content": string(raw), "truncated": truncated,
	})
}

// logs returns the serial console (or process stdio) and the guest agent log.
func (s *Server) logs(w http.ResponseWriter, id string) {
	job, ok := s.Store.Get(id)
	if !ok {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"console": tailFile(filepath.Join(job.TaskDir, "console.log"), 64<<10),
		"agent":   tailFile(filepath.Join(job.TaskDir, "agent.log"), 64<<10),
		"setup":   tailFile(filepath.Join(job.TaskDir, "setup.log"), 16<<10),
	})
}

func tailFile(path string, max int) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	if len(raw) > max {
		raw = raw[len(raw)-max:]
	}
	return string(raw)
}

// handleHelpers is the governed helper allowlist the console shows; the guest
// pipeline itself only ever runs the task's own test_commands.
func (s *Server) handleHelpers(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		writeErr(w, http.StatusUnauthorized, contract.ReasonUnauthorized, "X-Sandbox-Token rejected")
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"helpers": []map[string]any{
			{"group": "inspect", "commands": []string{"fs", "logs"}},
			{"group": "lifecycle", "commands": []string{"hold", "cancel", "release"}},
		},
	})
}

func (s *Server) release(w http.ResponseWriter, id string) {
	if !s.Store.Release(id) {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"released": true, "sandbox_task_id": id})
}

func (s *Server) heartbeat(w http.ResponseWriter, id string) {
	job, ok := s.Store.Heartbeat(id)
	if !ok {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	writeJSON(w, http.StatusOK, s.statusOf(job))
}

func (s *Server) hold(w http.ResponseWriter, r *http.Request, id string) {
	var body struct {
		TTLSeconds int    `json:"ttl_seconds"`
		Hours      int    `json:"hours"` // the console's hold body
		Reason     string `json:"reason"`
	}
	_ = json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&body)
	seconds := body.TTLSeconds
	if seconds <= 0 && body.Hours > 0 {
		seconds = body.Hours * 3600
	}
	ttl := time.Duration(seconds) * time.Second
	job, ok := s.Store.Hold(id, ttl)
	if !ok {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	writeJSON(w, http.StatusOK, s.statusOf(job))
}

func (s *Server) preview(w http.ResponseWriter) {
	// Preview is an execution claim. Fail closed while frozen / without Firecracker,
	// and stay honestly unsupported until the rootfs-preview tier is built.
	reason := contract.ReasonFirecrackerDisabled
	msg := "Firecracker backend is not enabled; preview is not claimed"
	switch {
	case s.Store.ClaimsFrozen():
		reason = contract.ReasonClaimsFrozen
		msg = "microVM execution is not claimed for Community GA (BOSS_MICROVM_CLAIMS_FROZEN=true)"
	case s.Runner != nil && s.Store.BackendLive():
		reason = contract.ReasonPreviewUnsupported
		msg = "rootfs preview is not implemented by this sandbox-service version; use fs/logs to inspect the retained workspace"
	}
	writeErr(w, http.StatusUnprocessableEntity, reason, msg)
}

func (s *Server) statusOf(j *job.Job) contract.StatusResponse {
	backendLive := s.Runner != nil && s.Store.BackendLive()
	return contract.StatusResponse{
		Status:          j.Status,
		Phase:           j.Phase,
		Reason:          j.Reason,
		Error:           j.Error,
		Lease:           j.LeaseView(),
		Result:          j.Result,
		Backend:         s.Store.BackendName(),
		FirecrackerLive: s.Store.FirecrackerLive(),
		CodingClaimed: backendLive && !s.Store.ClaimsFrozen() &&
			contract.EngineLooksLikeCoding(j.Engine),
	}
}

func writeErr(w http.ResponseWriter, status int, reason, message string) {
	writeJSON(w, status, contract.ErrorBody{Error: message, Reason: reason, Code: reason})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
