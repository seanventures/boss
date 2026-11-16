package gateway

import (
	"bufio"
	"context"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"net"
	"net/http"
	"net/http/cgi"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/guest"
	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanventures/boss/services/sandbox-service/internal/pki"
)

// P8b S11 (BOSSAPP-1072): git and REST leave the guest through boss-hostd to
// boss-egress with the HOST's proof, the guest's handle moved into
// X-Boss-Handle, the host's word on VM/run/epoch and the run's boss:egress
// token; git's credential helper is answered by the gateway itself.

func basic(user, pass string) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(user+":"+pass))
}

func TestGitForwardCarriesTheHostProofTheMovedHandleAndTheEgressToken(t *testing.T) {
	r := newRig(t)
	r.gw.BindRun("vm-a", "rep_a", "dep", "run_9", 4, map[string]any{"egr": "boss_egr_W"})
	m := &fakeMinter{ttl: 300}
	r.gw.Keys = &RunKeys{Mint: m.mint}
	if err := r.gw.Keys.Ensure(context.Background(), hostapi.Assignment{RunID: "run_9", JobID: "j", ReplicaID: "rep_a", Epoch: 4}); err != nil {
		t.Fatal(err)
	}
	jkt := r.gw.Keys.JKT("run_9")
	path := "/git/github.com/acme/web.git/info/refs"
	status, _ := r.call("vm-a", guest.PortGit, "GET", path+"?service=git-upload-pack", map[string]string{
		"Authorization": basic("boss", "boss_egr_W"), "X-Boss-Handle": "boss_egr_FORGED", "X-Boss-Vm": "vm-forged",
	})
	if status != 200 {
		t.Fatalf("git forward: %d %+v", status, r.lastCall())
	}
	h := r.seen[len(r.seen)-1]
	if h.Get("Authorization") != "BossHost proof-for-GET-"+path {
		t.Fatalf("the git forward must carry the host proof for the egress path, got %q", h.Get("Authorization"))
	}
	if h.Get("X-Boss-Handle") != "boss_egr_W" || h.Get("X-Boss-Vm") != "vm-a" || h.Get("X-Boss-Run") != "run_9" ||
		h.Get("X-Boss-Fence-Epoch") != "4" {
		t.Fatalf("the handle is moved and the binding is the host's word: %v", h)
	}
	tok := h.Get(hostapi.HeaderDelegation)
	if tok != "tok.boss:egress."+jkt {
		t.Fatalf("the git forward must carry the boss:egress token only, got %q", tok)
	}
	htu := NormalizeHTU("https", strings.TrimPrefix(r.up.URL, "https://"), path)
	if got, err := VerifyProof(h.Get(hostapi.HeaderDPoP), "GET", htu, tok, time.Now()); err != nil || got != jkt {
		t.Fatalf("the egress proof is over the forwarded URL: jkt=%q err=%v", got, err)
	}
	// REST takes the same contract.
	if status, _ := r.call("vm-a", guest.PortGit, "GET", "/api/github.com/repos/acme/web", map[string]string{"Authorization": "Bearer boss_egr_W"}); status != 200 {
		t.Fatalf("rest forward: %d", status)
	}
	if h := r.seen[len(r.seen)-1]; h.Get("X-Boss-Handle") != "boss_egr_W" || h.Get(hostapi.HeaderDelegation) != "tok.boss:egress."+jkt {
		t.Fatalf("rest forward headers: %v", h)
	}
	for _, rule := range DefaultRules() {
		if (rule.Route == RouteGit || rule.Route == RouteAPI) && !rule.Deny && !rule.Credential &&
			(!rule.HostProof || rule.Audience != hostapi.AudEgress) {
			t.Fatalf("git/api rule %s must carry the host proof and the boss:egress audience", rule.ID)
		}
	}
}

func TestGitWithoutAHandleIsChallengedNotForwarded(t *testing.T) {
	r := newRig(t)
	before := len(r.seen)
	c := &http.Client{Transport: &http.Transport{DialContext: func(context.Context, string, string) (net.Conn, error) {
		return net.Dial("unix", filepath.Join(r.dir, "vm-a_7102"))
	}}}
	resp, err := c.Get("http://127.0.0.1/git/github.com/acme/web.git/info/refs?service=git-upload-pack")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 401 || !strings.HasPrefix(resp.Header.Get("WWW-Authenticate"), "Basic") {
		t.Fatalf("git needs a Basic challenge to ask its helper: %d %q", resp.StatusCode, resp.Header.Get("WWW-Authenticate"))
	}
	if len(r.seen) != before {
		t.Fatal("a call with no handle is never forwarded")
	}
}

func TestTheCredentialHelperGetsOnlyThisVMsOwnHandle(t *testing.T) {
	r := newRig(t)
	before := len(r.seen)
	// vm-a holds run_7's handle (desired state): the helper's answer.
	status, body := r.call("vm-a", guest.PortGit, "GET", GitCredentialPath, nil)
	if status != 200 || body != "username=boss\npassword=boss_egr_A\n" {
		t.Fatalf("helper answer: %d %q", status, body)
	}
	if c := r.lastCall(); c.Verdict != "allow" || c.RunID != "run_7" || c.HandleFP != Fingerprint("boss_egr_A") {
		t.Fatalf("the helper call is audited by fingerprint, never the handle: %+v", c)
	}
	// vm-b holds no egress handle: nothing — and never vm-a's.
	status, body = r.call("vm-b", guest.PortGit, "GET", GitCredentialPath, nil)
	if status != 404 || strings.Contains(body, "boss_egr_") {
		t.Fatalf("another VM never learns a handle: %d %q", status, body)
	}
	// Two runs on one VM: the helper must be told which.
	r.gw.BindRun("vm-a", "rep_a", "dep", "run_9", 4, map[string]any{"egr": "boss_egr_W"})
	if status, _ := r.call("vm-a", guest.PortGit, "GET", GitCredentialPath, nil); status != 404 {
		t.Fatalf("two runs, no run named: %d", status)
	}
	if status, body := r.call("vm-a", guest.PortGit, "GET", GitCredentialPath+"?run=run_9", nil); status != 200 || !strings.Contains(body, "boss_egr_W") {
		t.Fatalf("run named: %d %q", status, body)
	}
	// A fenced run's handle is never handed out.
	r.gw.Update(hostapi.DesiredState{
		Endpoints: map[string]string{"egress": r.up.URL},
		VMs: []hostapi.DesiredVM{{ReplicaID: "rep_a", VMID: "vm-a", DeploymentID: "dep", Mode: hostapi.ModePerRun,
			Runs: []hostapi.RunBind{{RunID: "run_7", Epoch: 3, Handles: hostapi.Handles{Egress: "boss_egr_A"}}}}},
		Fences: []hostapi.Fence{{RunID: "run_7", MinEpoch: 4}},
	})
	r.gw.UnbindRun("run_9")
	if status, body := r.call("vm-a", guest.PortGit, "GET", GitCredentialPath, nil); status != 404 || strings.Contains(body, "boss_egr_") {
		t.Fatalf("fenced: %d %q", status, body)
	}
	if len(r.seen) != before {
		t.Fatal("the helper is answered by the host: never forwarded")
	}
}

func TestAnEgressHandleFromAnotherVMIsRefusedAndRevoked(t *testing.T) {
	r := newRig(t)
	status, _ := r.call("vm-b", guest.PortGit, "GET", "/git/github.com/acme/web.git/info/refs", map[string]string{"Authorization": basic("boss", "boss_egr_A")})
	if status != 403 || r.lastCall().RejectedBy != "handle_replay" {
		t.Fatalf("replay: %d %+v", status, r.lastCall())
	}
	if status, _ := r.call("vm-a", guest.PortGit, "GET", "/git/github.com/acme/web.git/info/refs", map[string]string{"Authorization": basic("boss", "boss_egr_A")}); status != 403 {
		t.Fatalf("a replayed handle is revoked for its own VM too: %d", status)
	}
}

// ---- the relay end to end (process backend) -------------------------------

// gitRun runs git (or another tool) with a fixed, proxy-free environment.
func gitRun(t *testing.T, env []string, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Dir, cmd.Env = dir, env
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%v: %v\n%s", args, err, out)
	}
	return string(out)
}

// A guest clone, fetch and push work through boss-vsockd's relay (the process
// backend's Unix sockets), the host gateway and an egress stand-in serving
// `git http-backend`; git's config holds no token and no handle, and every
// forward carried the host's proof and the moved handle.
func TestGuestCloneFetchPushThroughTheRelay(t *testing.T) {
	gitBin, err := exec.LookPath("git")
	if err != nil {
		t.Skip("no git")
	}
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("no python3")
	}
	_, self, _, _ := runtime.Caller(0)
	agentPy := filepath.Join(filepath.Dir(self), "..", "backend", "guest", "in_guest_agent.py")
	root := shortTemp(t)
	home := filepath.Join(root, "home")
	_ = os.MkdirAll(home, 0o700)
	env := []string{"HOME=" + home, "PATH=" + os.Getenv("PATH"), "GIT_CONFIG_NOSYSTEM=1",
		"GIT_TERMINAL_PROMPT=0", "GIT_AUTHOR_NAME=a", "GIT_AUTHOR_EMAIL=a@a", "GIT_COMMITTER_NAME=a", "GIT_COMMITTER_EMAIL=a@a"}

	// The repository "at GitHub": a bare repo with one commit on main.
	bare := filepath.Join(root, "hub", "acme", "web.git")
	gitRun(t, env, root, gitBin, "init", "-q", "--bare", "-b", "main", bare)
	gitRun(t, env, root, gitBin, "-C", bare, "config", "http.receivepack", "true")
	seed := filepath.Join(root, "seed")
	gitRun(t, env, root, gitBin, "init", "-q", "-b", "main", seed)
	_ = os.WriteFile(filepath.Join(seed, "README.md"), []byte("hello\n"), 0o644)
	gitRun(t, env, seed, gitBin, "add", "-A")
	gitRun(t, env, seed, gitBin, "commit", "-q", "-m", "seed")
	gitRun(t, env, seed, gitBin, "push", "-q", bare, "main")

	// The egress stand-in: what boss-egress receives on its host route.
	var mu sync.Mutex
	var seen []http.Header
	backend := &cgi.Handler{Path: gitBin, Args: []string{"http-backend"},
		Env: []string{"GIT_PROJECT_ROOT=" + filepath.Join(root, "hub"), "GIT_HTTP_EXPORT_ALL=1"}}
	egress := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		mu.Lock()
		seen = append(seen, req.Header.Clone())
		mu.Unlock()
		if !strings.HasPrefix(req.Header.Get("Authorization"), "BossHost ") || req.Header.Get("X-Boss-Handle") != "boss_egr_G" ||
			req.Header.Get("X-Boss-Vm") != "vm-g" || req.Header.Get("X-Boss-Run") != "run_g" {
			http.Error(w, "not a host forward", http.StatusUnauthorized)
			return
		}
		rest, ok := strings.CutPrefix(req.URL.Path, "/git/github.com")
		if !ok {
			http.Error(w, "unrouted", http.StatusForbidden)
			return
		}
		req.URL.Path = rest
		backend.ServeHTTP(w, req)
	}))
	t.Cleanup(egress.Close)

	// boss-hostd's gateway for one per_run VM.
	ca, _ := pki.NewCA("root")
	sk, _ := pki.GenerateKey()
	csrPEM, _ := pki.CSR(sk, "svid-ca")
	csr, _ := pki.ParseCSR(csrPEM)
	der, _ := ca.SignSVIDCA(csr, "inst.boss", "hst_1", time.Hour)
	cert, _ := x509.ParseCertificate(der)
	creds := &pki.HostCreds{HostID: "hst_1", TrustDomain: "inst.boss", SVIDCAKey: sk, SVIDCACert: cert}
	pool := x509.NewCertPool()
	pool.AddCert(ca.Cert)
	gw := New("hst_1", pki.NewMinter(func() *pki.HostCreds { return creds }), pool)
	gw.HostProof = func(method, path string) (string, error) { return "proof-" + method + "-" + path, nil }
	gw.Update(hostapi.DesiredState{
		Endpoints: map[string]string{"egress": egress.URL},
		VMs: []hostapi.DesiredVM{{ReplicaID: "rep_g", VMID: "vm-g", DeploymentID: "dep", Mode: hostapi.ModePerRun,
			Runs: []hostapi.RunBind{{RunID: "run_g", Epoch: 1, Handles: hostapi.Handles{Egress: "boss_egr_G"}}}}},
	})
	vmDir := filepath.Join(root, "vm")
	_ = os.MkdirAll(vmDir, 0o700)
	if err := gw.Attach("vm-g", VMInfo{DeploymentID: "dep"}, func(port int) (net.Listener, error) {
		return net.Listen("unix", guest.UnixGuestPath(vmDir, port))
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { gw.Detach("vm-g") })

	// The guest: boss-vsockd's relay (loopback TCP → the VM's Unix sockets).
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	d := &guest.Daemon{T: guest.UnixTransport{Dir: vmDir}, NoInbox: true}
	go func() { _ = d.Run(ctx) }()
	var gitPort int
	for i := 0; i < 100 && gitPort == 0; i++ {
		if c, err := net.Dial("unix", guest.UnixControlPath(vmDir)); err == nil {
			_, _ = c.Write([]byte(`{"op":"ping"}` + "\n"))
			line, _ := bufio.NewReader(c).ReadBytes('\n')
			c.Close()
			var resp guest.Response
			if json.Unmarshal(line, &resp) == nil {
				gitPort = resp.Ports[guest.PortGit]
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	if gitPort == 0 {
		t.Fatal("the guest relay never came up")
	}

	// The guest's git configuration, by the in-guest agent's own code.
	spec := `{"endpoints":{"git":"http://127.0.0.1:` + itoa(gitPort) + `"},"handles":{"egr":"boss_egr_G"}}`
	gitRun(t, env, root, py, "-I", "-c",
		"import importlib.util,json,sys; s=importlib.util.spec_from_file_location('a',sys.argv[1]); "+
			"m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.configure_git_relay(json.loads(sys.argv[2]))",
		agentPy, spec)

	work := filepath.Join(root, "work")
	gitRun(t, env, root, gitBin, "clone", "-q", "https://github.com/acme/web.git", work)
	if b, _ := os.ReadFile(filepath.Join(work, "README.md")); string(b) != "hello\n" {
		t.Fatalf("clone through the relay: %q", b)
	}
	_ = os.WriteFile(filepath.Join(work, "change.txt"), []byte("x\n"), 0o644)
	gitRun(t, env, work, gitBin, "add", "-A")
	gitRun(t, env, work, gitBin, "commit", "-q", "-m", "change")
	gitRun(t, env, work, gitBin, "push", "-q", "origin", "HEAD:refs/heads/boss/run_g-1")
	gitRun(t, env, work, gitBin, "fetch", "-q", "origin")
	if out := gitRun(t, env, root, gitBin, "-C", bare, "branch", "--list", "boss/run_g-1"); !strings.Contains(out, "boss/run_g-1") {
		t.Fatalf("the push reached the remote: %q", out)
	}

	// No token, no handle: not in any git config, not in the environment.
	for _, f := range []string{filepath.Join(home, ".gitconfig"), filepath.Join(work, ".git", "config")} {
		b, _ := os.ReadFile(f)
		if strings.Contains(string(b), "boss_egr_") || strings.Contains(string(b), "x-access-token") {
			t.Fatalf("%s holds a credential:\n%s", f, b)
		}
	}
	for _, kv := range env {
		if strings.Contains(kv, "boss_egr_") {
			t.Fatal("the environment holds the handle")
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) < 4 {
		t.Fatalf("clone + push + fetch made %d forwards", len(seen))
	}
	for _, h := range seen {
		if strings.HasPrefix(h.Get("Authorization"), "Basic") || h.Get("X-Boss-Handle") != "boss_egr_G" {
			t.Fatalf("every forward carries the host proof and the moved handle, never the guest's Basic: %v", h)
		}
	}
}

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}

func shortTemp(t *testing.T) string {
	dir, err := os.MkdirTemp("", "gg")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}
