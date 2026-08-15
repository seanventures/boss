// Package httpapi serves the MicroVMProvider HTTP contract.
// It never boots Firecracker. Claims freeze and missing backend fail closed.
package httpapi

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
)

type Server struct {
	Store *job.Store
	Token string
	mux   *http.ServeMux
}

func New(store *job.Store, token string) *Server {
	s := &Server{Store: store, Token: token, mux: http.NewServeMux()}
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/v1/sandbox/health", s.handleHealth)
	s.mux.HandleFunc("/v1/sandbox/tasks", s.handleTasks)
	s.mux.HandleFunc("/v1/sandbox/tasks/", s.handleTask)
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
	return r.Header.Get("X-Sandbox-Token") == s.Token
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	frozen := s.Store.ClaimsFrozen()
	live := s.Store.FirecrackerLive()
	reason := ""
	if frozen {
		reason = contract.ReasonClaimsFrozen
	} else if !live {
		reason = contract.ReasonFirecrackerDisabled
	}
	// ok is true only when Firecracker is actually live and claims are not frozen.
	// The in-repo scaffold never sets this. Do not invent host.connected=true.
	body := map[string]any{
		"ok":                false,
		"service":           "up",
		"backend":           "none",
		"firecracker_live":  false,
		"execution_ready":   false,
		"execution_claimed": false,
		"claims_frozen":     frozen,
		"reason":            reason,
		"contract_version":  contract.Version,
		"capacity":          0,
		"available":         0,
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
	writeJSON(w, http.StatusOK, statusOf(job))
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
	writeJSON(w, http.StatusOK, statusOf(job))
}

func (s *Server) hold(w http.ResponseWriter, r *http.Request, id string) {
	var body struct {
		TTLSeconds int    `json:"ttl_seconds"`
		Reason     string `json:"reason"`
	}
	_ = json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&body)
	ttl := time.Duration(body.TTLSeconds) * time.Second
	job, ok := s.Store.Hold(id, ttl)
	if !ok {
		writeErr(w, http.StatusNotFound, contract.ReasonTaskMissing, "sandbox not found")
		return
	}
	writeJSON(w, http.StatusOK, statusOf(job))
}

func (s *Server) preview(w http.ResponseWriter) {
	// Preview is an execution claim. Fail closed while frozen / without Firecracker.
	reason := contract.ReasonFirecrackerDisabled
	msg := "Firecracker backend is not enabled; preview is not claimed"
	if s.Store.ClaimsFrozen() {
		reason = contract.ReasonClaimsFrozen
		msg = "microVM execution is not claimed for Community GA (BOSS_MICROVM_CLAIMS_FROZEN=true)"
	}
	writeErr(w, http.StatusUnprocessableEntity, reason, msg)
}

func statusOf(j *job.Job) contract.StatusResponse {
	return contract.StatusResponse{
		Status:          j.Status,
		Phase:           j.Phase,
		Reason:          j.Reason,
		Error:           j.Error,
		Lease:           j.LeaseView(),
		Result:          j.Result,
		Backend:         "none",
		FirecrackerLive: false,
		CodingClaimed:   false,
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
