package guest

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"os"
	"testing"
	"time"
)

func TestInboxStandInProtocol(t *testing.T) {
	dir, err := os.MkdirTemp("", "ib")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	d := &Daemon{T: UnixTransport{Dir: dir}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = d.Run(ctx) }()
	for i := 0; i < 100; i++ {
		if _, err := os.Stat(UnixInboxPath(dir)); err == nil {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if info, err := os.Stat(UnixInboxPath(dir)); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("inbox socket: %v", err)
	}
	c, err := net.Dial("unix", UnixInboxPath(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	rd := bufio.NewReader(c)
	ask := func(line string) InboxReply {
		_, _ = c.Write([]byte(line + "\n"))
		raw, err := rd.ReadBytes('\n')
		if err != nil {
			t.Fatal(err)
		}
		var r InboxReply
		_ = json.Unmarshal(raw, &r)
		return r
	}
	if r := ask(`{"op":"run","run_id":"r0"}`); r.OK || r.Reason != "draining" {
		t.Fatalf("before bootstrap: %+v", r)
	}
	d.mu.Lock()
	d.boot = &Bootstrap{VMID: "v", Mode: "replica", Slots: 1, RunHoldMS: 60000}
	d.mu.Unlock()
	// One connection, many lines.
	if r := ask(`{"op":"slots"}`); !r.OK || *r.BusySlots != 0 || *r.TotalSlots != 1 {
		t.Fatalf("slots: %+v", r)
	}
	if r := ask(`{"op":"run","run_id":"r1","job_id":"j","replica_id":"rep","epoch":1}`); !r.OK {
		t.Fatalf("run: %+v", r)
	}
	if r := ask(`{"op":"run","run_id":"r1"}`); r.OK || r.Reason != "duplicate" {
		t.Fatalf("duplicate: %+v", r)
	}
	if r := ask(`{"op":"run","run_id":"r2"}`); r.OK || r.Reason != "no_free_slot" {
		t.Fatalf("full: %+v", r)
	}
	if r := ask(`{"op":"ping"}`); !r.OK || *r.BusySlots != 1 {
		t.Fatalf("ping: %+v", r)
	}
	if r := ask(`not json`); r.OK || r.Reason != "bad_request" {
		t.Fatalf("bad: %+v", r)
	}
}
