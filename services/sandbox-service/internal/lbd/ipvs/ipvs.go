// Package ipvs programs the kernel's IP Virtual Server over generic netlink
// (the IPVS genl family), the way ipvsadm and kube-proxy do. It exists
// because moby/ipvs v1.1.0 cannot set a destination's tunnel type, and the
// directors must use GRE tunnel mode (IPVS_DEST_ATTR_TUN_TYPE = GRE):
// the host's nftables learn the caller flow from GRE's inner headers
// (docs/infra/HOST_API.md §5.1, PR #102).
//
// Backend is the small surface boss-lbd needs; Fake is the in-memory one the
// reconcile tests run against, and Netlink (linux) is the kernel.
package ipvs

import (
	"fmt"
	"net"
	"sort"
	"sync"
)

// Forwarding methods (IP_VS_CONN_F_FWD_MASK values).
const (
	FwdMasq   uint32 = 0
	FwdTunnel uint32 = 2
	FwdDRoute uint32 = 3
)

// Tunnel types (IP_VS_CONN_F_TUNNEL_TYPE_*), kernel ≥ 5.2 for GUE/GRE.
const (
	TunIPIP uint8 = 0
	TunGUE  uint8 = 1
	TunGRE  uint8 = 2
)

// Sync daemon states.
const (
	DaemonMaster uint32 = 1
	DaemonBackup uint32 = 2
)

// Service is one virtual service: (protocol, address, port).
type Service struct {
	Address   net.IP
	Port      uint16
	Protocol  uint16 // 6 = TCP
	Scheduler string // rr | wlc | …
	Flags     uint32 // persistence etc.; 0 = none
	Timeout   uint32 // persistence timeout; 0
	Netmask   uint32 // 0xffffffff for IPv4
}

// Key identifies a service: "tcp/203.0.113.17:443".
func (s Service) Key() string {
	proto := "tcp"
	if s.Protocol == 17 {
		proto = "udp"
	}
	return fmt.Sprintf("%s/%s:%d", proto, s.Address, s.Port)
}

// Destination is one real server of a service.
type Destination struct {
	Address     net.IP
	Port        uint16
	Weight      int
	Forward     uint32 // FwdTunnel
	TunType     uint8  // TunGRE
	ActiveConns uint32
	InactConns  uint32
}

// Key identifies a destination inside its service: "10.200.4.7:443".
func (d Destination) Key() string { return fmt.Sprintf("%s:%d", d.Address, d.Port) }

// Stats is the global IPVS counter set (/proc/net/ip_vs_stats).
type Stats struct {
	Conns, PacketsIn, PacketsOut, BytesIn, BytesOut uint64
	CPS, PPSIn, PPSOut, BPSIn, BPSOut               uint64
}

// Backend is what boss-lbd programs.
type Backend interface {
	Services() ([]Service, error)
	Destinations(s Service) ([]Destination, error)
	AddService(s Service) error
	UpdateService(s Service) error
	DelService(s Service) error
	AddDestination(s Service, d Destination) error
	UpdateDestination(s Service, d Destination) error
	DelDestination(s Service, d Destination) error
	// StartDaemon starts the connection sync daemon in state (master or
	// backup) on iface with syncID; StopDaemon stops that state's daemon.
	StartDaemon(state uint32, iface string, syncID uint32) error
	StopDaemon(state uint32) error
	Stats() (Stats, error)
	Close()
}

// Fake is an in-memory Backend that records every call (tests).
type Fake struct {
	mu       sync.Mutex
	services map[string]Service
	dests    map[string]map[string]Destination
	Ops      []string
	Daemons  map[uint32]string // state → iface
	// Fail makes the named op return an error (tests of partial failure).
	Fail map[string]error
}

// NewFake is an empty fake kernel table.
func NewFake() *Fake {
	return &Fake{services: map[string]Service{}, dests: map[string]map[string]Destination{},
		Daemons: map[uint32]string{}, Fail: map[string]error{}}
}

func (f *Fake) op(name string, args ...any) error {
	f.Ops = append(f.Ops, fmt.Sprintf(name, args...))
	if err, ok := f.Fail[name]; ok {
		return err
	}
	return nil
}

func (f *Fake) Services() ([]Service, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]Service, 0, len(f.services))
	for _, s := range f.services {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key() < out[j].Key() })
	return out, nil
}

func (f *Fake) Destinations(s Service) ([]Destination, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.services[s.Key()]; !ok {
		return nil, fmt.Errorf("no such service %s", s.Key())
	}
	out := make([]Destination, 0)
	for _, d := range f.dests[s.Key()] {
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key() < out[j].Key() })
	return out, nil
}

func (f *Fake) AddService(s Service) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("add-service %s %s", s.Key(), s.Scheduler); err != nil {
		return err
	}
	if _, ok := f.services[s.Key()]; ok {
		return fmt.Errorf("service %s exists", s.Key())
	}
	f.services[s.Key()] = s
	f.dests[s.Key()] = map[string]Destination{}
	return nil
}

func (f *Fake) UpdateService(s Service) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("update-service %s %s", s.Key(), s.Scheduler); err != nil {
		return err
	}
	if _, ok := f.services[s.Key()]; !ok {
		return fmt.Errorf("no such service %s", s.Key())
	}
	f.services[s.Key()] = s
	return nil
}

func (f *Fake) DelService(s Service) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("del-service %s", s.Key()); err != nil {
		return err
	}
	delete(f.services, s.Key())
	delete(f.dests, s.Key())
	return nil
}

func (f *Fake) AddDestination(s Service, d Destination) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("add-dest %s %s w=%d fwd=%d tun=%d", s.Key(), d.Key(), d.Weight, d.Forward, d.TunType); err != nil {
		return err
	}
	m, ok := f.dests[s.Key()]
	if !ok {
		return fmt.Errorf("no such service %s", s.Key())
	}
	if _, dup := m[d.Key()]; dup {
		return fmt.Errorf("dest %s exists", d.Key())
	}
	m[d.Key()] = d
	return nil
}

func (f *Fake) UpdateDestination(s Service, d Destination) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("update-dest %s %s w=%d", s.Key(), d.Key(), d.Weight); err != nil {
		return err
	}
	m, ok := f.dests[s.Key()]
	if !ok {
		return fmt.Errorf("no such service %s", s.Key())
	}
	if _, has := m[d.Key()]; !has {
		return fmt.Errorf("no such dest %s", d.Key())
	}
	m[d.Key()] = d
	return nil
}

func (f *Fake) DelDestination(s Service, d Destination) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("del-dest %s %s", s.Key(), d.Key()); err != nil {
		return err
	}
	if m, ok := f.dests[s.Key()]; ok {
		delete(m, d.Key())
	}
	return nil
}

func (f *Fake) StartDaemon(state uint32, iface string, syncID uint32) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.op("start-daemon %d %s %d", state, iface, syncID); err != nil {
		return err
	}
	f.Daemons[state] = iface
	return nil
}

func (f *Fake) StopDaemon(state uint32) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.Daemons, state)
	return f.op("stop-daemon %d", state)
}

func (f *Fake) Stats() (Stats, error) { return Stats{}, nil }
func (f *Fake) Close()                {}

// Table is a snapshot "svcKey → destKey → weight" (tests).
func (f *Fake) Table() map[string]map[string]int {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := map[string]map[string]int{}
	for k := range f.services {
		out[k] = map[string]int{}
		for dk, d := range f.dests[k] {
			out[k][dk] = d.Weight
		}
	}
	return out
}
