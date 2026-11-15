//go:build linux

package lbd

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// GREProber sends a TCP SYN inside a GRE packet to the replica's vm_ip
// (inner dst = VIP:port, inner src = the director's address, a source port
// from a range outside the kernel's ephemeral range) and waits for the
// VM's SYN-ACK, which returns direct (DSR) to the director's address. The
// kernel, having no socket for it, answers RST, which also closes the
// half-open connection on the VM. This is the exact data path IPVS uses, so
// a passing check means: GRE accepted by the host's nftables, decapsulated
// in the guest, the VIP held on the guest, the listener up.
//
// Needs CAP_NET_RAW, and on the director interface accept_local=1 and
// rp_filter=0 (a MASTER holds the VIP, so a SYN-ACK from the VIP has a local
// source address). Agent.sysctls sets both.
type GREProber struct {
	Local net.IP // the director's address (inner source)

	once    sync.Once
	initErr error
	send    int
	recv    int
	next    atomic.Uint32
	mu      sync.Mutex
	waiting map[uint16]chan uint32 // sport → ack number seen
	ports   map[string]uint16      // member → its fixed source port (one learned flow per member on the host)
}

const (
	probePortBase  = 61000
	probePortRange = 4000 // 61000..64999, above a default ip_local_port_range
)

func (g *GREProber) init() {
	g.initErr = func() error {
		if g.Local.To4() == nil {
			return errors.New("gre prober: an IPv4 local address is required")
		}
		s, err := unix.Socket(unix.AF_INET, unix.SOCK_RAW|unix.SOCK_CLOEXEC, unix.IPPROTO_GRE)
		if err != nil {
			return fmt.Errorf("raw GRE socket (CAP_NET_RAW): %w", err)
		}
		g.send = s
		r, err := unix.Socket(unix.AF_INET, unix.SOCK_RAW|unix.SOCK_CLOEXEC, unix.IPPROTO_TCP)
		if err != nil {
			return fmt.Errorf("raw TCP socket: %w", err)
		}
		g.recv = r
		// Classic BPF: only TCP packets whose destination port is in our
		// probe range reach user space (the rest is dropped in the kernel).
		//   ldb [0]; and 0xf; lsh 2; tax            x = ihl*4
		//   ldh [x+2]                                tcp dport
		//   jge 61000 ? next : drop
		//   jgt 64999 ? drop : accept
		filter := []unix.SockFilter{
			{Code: unix.BPF_LD | unix.BPF_B | unix.BPF_ABS, K: 0},
			{Code: unix.BPF_ALU | unix.BPF_AND | unix.BPF_K, K: 0x0f},
			{Code: unix.BPF_ALU | unix.BPF_LSH | unix.BPF_K, K: 2},
			{Code: unix.BPF_MISC | unix.BPF_TAX},
			{Code: unix.BPF_LD | unix.BPF_H | unix.BPF_IND, K: 2},
			{Code: unix.BPF_JMP | unix.BPF_JGE | unix.BPF_K, K: probePortBase, Jt: 0, Jf: 2},
			{Code: unix.BPF_JMP | unix.BPF_JGT | unix.BPF_K, K: probePortBase + probePortRange - 1, Jt: 1, Jf: 0},
			{Code: unix.BPF_RET | unix.BPF_K, K: 0xffff},
			{Code: unix.BPF_RET | unix.BPF_K, K: 0},
		}
		prog := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
		if err := unix.SetsockoptSockFprog(r, unix.SOL_SOCKET, unix.SO_ATTACH_FILTER, &prog); err != nil {
			return fmt.Errorf("attach BPF: %w", err)
		}
		g.waiting = map[uint16]chan uint32{}
		g.ports = map[string]uint16{}
		go g.receive()
		return nil
	}()
}

// sportFor gives each (vm_ip, vip, port) one fixed source port, so the
// host's learned-flow set holds ONE entry per director per member instead
// of one per probe, and a probe's RST closes exactly the flow it opened.
func (g *GREProber) sportFor(key string) uint16 {
	g.mu.Lock()
	defer g.mu.Unlock()
	if p, ok := g.ports[key]; ok {
		return p
	}
	p := uint16(probePortBase + int(g.next.Add(1)%probePortRange))
	g.ports[key] = p
	return p
}

// Probe implements Prober.
func (g *GREProber) Probe(ctx context.Context, vmIP, vip string, port int) error {
	g.once.Do(g.init)
	if g.initErr != nil {
		return g.initErr
	}
	outer := net.ParseIP(vmIP).To4()
	inner := net.ParseIP(vip).To4()
	if outer == nil || inner == nil {
		return errors.New("IPv4 only")
	}
	sport := g.sportFor(vmIP + "|" + vip + "|" + strconv.Itoa(port))
	var seqb [4]byte
	_, _ = rand.Read(seqb[:])
	seq := binary.BigEndian.Uint32(seqb[:])
	ch := make(chan uint32, 8)
	g.mu.Lock()
	g.waiting[sport] = ch
	g.mu.Unlock()
	defer func() {
		g.mu.Lock()
		delete(g.waiting, sport)
		g.mu.Unlock()
	}()
	pkt := buildGRESyn(g.Local.To4(), inner, sport, uint16(port), seq)
	addr := &unix.SockaddrInet4{}
	copy(addr.Addr[:], outer)
	if err := unix.Sendto(g.send, pkt, 0, addr); err != nil {
		return fmt.Errorf("send GRE SYN to %s: %w", vmIP, err)
	}
	for {
		select {
		case ack := <-ch:
			if ack != seq+1 {
				// A stale SYN-ACK: a half-open connection from an earlier probe
				// that timed out is retransmitting. Close it (RST at the seq it
				// expects) and keep waiting for ours, or every later probe on
				// this fixed port would be poisoned by the retransmits.
				_ = unix.Sendto(g.send, buildGRESeg(g.Local.To4(), inner, sport, uint16(port), ack, 0x04), 0, addr)
				continue
			}
			// Close what we opened: an RST through the same GRE path frees the
			// VM's half-open connection at once (the director kernel's own RST
			// to the VIP never reaches the VM: the VIP is local or routed away).
			rst := buildGRESeg(g.Local.To4(), inner, sport, uint16(port), seq+1, 0x04)
			_ = unix.Sendto(g.send, rst, 0, addr)
			return nil
		case <-ctx.Done():
			return fmt.Errorf("no SYN-ACK from %s via %s:%d", vip, vmIP, port)
		}
	}
}

func (g *GREProber) receive() {
	buf := make([]byte, 2048)
	for {
		n, _, err := unix.Recvfrom(g.recv, buf, 0)
		if err != nil {
			if errors.Is(err, unix.EINTR) || errors.Is(err, unix.EAGAIN) {
				continue
			}
			time.Sleep(10 * time.Millisecond)
			continue
		}
		if n < 20 {
			continue
		}
		ihl := int(buf[0]&0x0f) * 4
		if n < ihl+20 {
			continue
		}
		tcp := buf[ihl:n]
		dport := binary.BigEndian.Uint16(tcp[2:4])
		flags := tcp[13]
		if flags&0x12 != 0x12 { // SYN+ACK
			continue
		}
		ack := binary.BigEndian.Uint32(tcp[8:12])
		g.mu.Lock()
		ch := g.waiting[dport]
		g.mu.Unlock()
		if ch != nil {
			select {
			case ch <- ack:
			default:
			}
		}
	}
}

// buildGRESyn is GRE(proto 0x0800) + IPv4 + TCP SYN; the raw GRE socket
// adds the outer IPv4 header.
func buildGRESyn(src, dst net.IP, sport, dport uint16, seq uint32) []byte {
	return buildGRESeg(src, dst, sport, dport, seq, 0x02)
}

// buildGRESeg is one TCP segment (flags) with no payload inside GRE + IPv4.
func buildGRESeg(src, dst net.IP, sport, dport uint16, seq uint32, flags byte) []byte {
	tcp := make([]byte, 20)
	binary.BigEndian.PutUint16(tcp[0:2], sport)
	binary.BigEndian.PutUint16(tcp[2:4], dport)
	binary.BigEndian.PutUint32(tcp[4:8], seq)
	tcp[12] = 5 << 4 // data offset 5 words
	tcp[13] = flags
	binary.BigEndian.PutUint16(tcp[14:16], 65535)
	pseudo := make([]byte, 12)
	copy(pseudo[0:4], src)
	copy(pseudo[4:8], dst)
	pseudo[9] = unix.IPPROTO_TCP
	binary.BigEndian.PutUint16(pseudo[10:12], uint16(len(tcp)))
	binary.BigEndian.PutUint16(tcp[16:18], checksum(append(pseudo, tcp...)))

	ip := make([]byte, 20)
	ip[0] = 0x45
	binary.BigEndian.PutUint16(ip[2:4], uint16(20+len(tcp)))
	binary.BigEndian.PutUint16(ip[4:6], uint16(seq&0xffff))
	ip[8] = 64
	ip[9] = unix.IPPROTO_TCP
	copy(ip[12:16], src)
	copy(ip[16:20], dst)
	binary.BigEndian.PutUint16(ip[10:12], checksum(ip))

	gre := []byte{0x00, 0x00, 0x08, 0x00}
	out := make([]byte, 0, len(gre)+len(ip)+len(tcp))
	out = append(out, gre...)
	out = append(out, ip...)
	return append(out, tcp...)
}

func checksum(b []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(b); i += 2 {
		sum += uint32(binary.BigEndian.Uint16(b[i : i+2]))
	}
	if len(b)%2 == 1 {
		sum += uint32(b[len(b)-1]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}

var _ = unsafe.Sizeof(0)
