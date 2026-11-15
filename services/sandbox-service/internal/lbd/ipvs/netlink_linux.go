//go:build linux

package ipvs

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"

	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netlink/nl"
	"golang.org/x/sys/unix"
)

// include/uapi/linux/ip_vs.h
const (
	genlVersion = 1

	cmdNewService uint8 = 1
	cmdSetService uint8 = 2
	cmdDelService uint8 = 3
	cmdGetService uint8 = 4
	cmdNewDest    uint8 = 5
	cmdSetDest    uint8 = 6
	cmdDelDest    uint8 = 7
	cmdGetDest    uint8 = 8
	cmdNewDaemon  uint8 = 9
	cmdDelDaemon  uint8 = 10

	attrService = 1
	attrDest    = 2
	attrDaemon  = 3

	svcAF       = 1
	svcProtocol = 2
	svcAddr     = 3
	svcPort     = 4
	svcFwmark   = 5
	svcSched    = 6
	svcFlags    = 7
	svcTimeout  = 8
	svcNetmask  = 9

	destAddr       = 1
	destPort       = 2
	destFwd        = 3
	destWeight     = 4
	destUThresh    = 5
	destLThresh    = 6
	destActive     = 7
	destInact      = 8
	destAF         = 11
	destTunType    = 13
	destTunPort    = 14
	destTunFlags   = 15
	daemonState    = 1
	daemonMcastIfn = 2
	daemonSyncID   = 3
)

// Netlink is the kernel IPVS table.
type Netlink struct {
	mu     sync.Mutex
	family uint16
	seq    uint32
}

// Open loads ip_vs (modprobe, best effort) and resolves the genl family.
func Open() (*Netlink, error) {
	_ = exec.Command("modprobe", "-q", "ip_vs").Run()
	for _, m := range []string{"ip_vs_rr", "ip_vs_wlc"} {
		_ = exec.Command("modprobe", "-q", m).Run()
	}
	fam, err := netlink.GenlFamilyGet("IPVS")
	if err != nil {
		return nil, fmt.Errorf("ipvs genl family (is ip_vs loaded?): %w", err)
	}
	return &Netlink{family: fam.ID}, nil
}

func (n *Netlink) Close() {}

func (n *Netlink) request(cmd uint8, flags int) *nl.NetlinkRequest {
	req := nl.NewNetlinkRequest(int(n.family), unix.NLM_F_ACK|flags)
	req.AddData(&nl.Genlmsg{Command: cmd, Version: genlVersion})
	return req
}

func be16(v uint16) []byte {
	b := make([]byte, 2)
	binary.BigEndian.PutUint16(b, v)
	return b
}

func serviceAttr(s Service) *nl.RtAttr {
	a := nl.NewRtAttr(attrService, nil)
	ip4 := s.Address.To4()
	if ip4 == nil {
		return nil
	}
	nl.NewRtAttrChild(a, svcAF, nl.Uint16Attr(unix.AF_INET))
	proto := s.Protocol
	if proto == 0 {
		proto = unix.IPPROTO_TCP
	}
	nl.NewRtAttrChild(a, svcProtocol, nl.Uint16Attr(proto))
	nl.NewRtAttrChild(a, svcAddr, []byte(ip4))
	nl.NewRtAttrChild(a, svcPort, be16(s.Port))
	return a
}

func fullServiceAttr(s Service) *nl.RtAttr {
	a := serviceAttr(s)
	if a == nil {
		return nil
	}
	sched := s.Scheduler
	if sched == "" {
		sched = "rr"
	}
	nl.NewRtAttrChild(a, svcSched, nl.ZeroTerminated(sched))
	flags := make([]byte, 8) // struct ip_vs_flags { __u32 flags; __u32 mask; }
	nl.NativeEndian().PutUint32(flags[0:4], s.Flags)
	nl.NativeEndian().PutUint32(flags[4:8], 0xffffffff)
	nl.NewRtAttrChild(a, svcFlags, flags)
	nl.NewRtAttrChild(a, svcTimeout, nl.Uint32Attr(s.Timeout))
	mask := s.Netmask
	if mask == 0 {
		mask = 0xffffffff
	}
	nl.NewRtAttrChild(a, svcNetmask, nl.Uint32Attr(mask))
	return a
}

func destAttr(d Destination, full bool) *nl.RtAttr {
	a := nl.NewRtAttr(attrDest, nil)
	ip4 := d.Address.To4()
	if ip4 == nil {
		return nil
	}
	nl.NewRtAttrChild(a, destAddr, []byte(ip4))
	nl.NewRtAttrChild(a, destPort, be16(d.Port))
	if full {
		nl.NewRtAttrChild(a, destFwd, nl.Uint32Attr(d.Forward&0x7))
		w := d.Weight
		if w < 0 {
			w = 0
		}
		nl.NewRtAttrChild(a, destWeight, nl.Uint32Attr(uint32(w)))
		nl.NewRtAttrChild(a, destUThresh, nl.Uint32Attr(0))
		nl.NewRtAttrChild(a, destLThresh, nl.Uint32Attr(0))
		nl.NewRtAttrChild(a, destAF, nl.Uint16Attr(unix.AF_INET))
		if d.Forward&0x7 == FwdTunnel {
			nl.NewRtAttrChild(a, destTunType, nl.Uint8Attr(d.TunType))
			nl.NewRtAttrChild(a, destTunPort, be16(0))
			nl.NewRtAttrChild(a, destTunFlags, nl.Uint16Attr(0))
		}
	}
	return a
}

func (n *Netlink) exec(req *nl.NetlinkRequest) ([][]byte, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	return req.Execute(unix.NETLINK_GENERIC, 0)
}

func (n *Netlink) cmdService(cmd uint8, s Service) error {
	req := n.request(cmd, 0)
	a := fullServiceAttr(s)
	if a == nil {
		return fmt.Errorf("service %s: IPv4 only", s.Address)
	}
	if cmd == cmdDelService {
		a = serviceAttr(s)
	}
	req.AddData(a)
	_, err := n.exec(req)
	return err
}

func (n *Netlink) cmdDest(cmd uint8, s Service, d Destination) error {
	req := n.request(cmd, 0)
	sa := serviceAttr(s)
	da := destAttr(d, cmd != cmdDelDest)
	if sa == nil || da == nil {
		return errors.New("IPv4 only")
	}
	req.AddData(sa)
	req.AddData(da)
	_, err := n.exec(req)
	return err
}

func (n *Netlink) AddService(s Service) error    { return n.cmdService(cmdNewService, s) }
func (n *Netlink) UpdateService(s Service) error { return n.cmdService(cmdSetService, s) }
func (n *Netlink) DelService(s Service) error    { return n.cmdService(cmdDelService, s) }

func (n *Netlink) AddDestination(s Service, d Destination) error {
	return n.cmdDest(cmdNewDest, s, d)
}
func (n *Netlink) UpdateDestination(s Service, d Destination) error {
	return n.cmdDest(cmdSetDest, s, d)
}
func (n *Netlink) DelDestination(s Service, d Destination) error {
	return n.cmdDest(cmdDelDest, s, d)
}

// Services dumps every virtual service (IPv4 ones are returned).
func (n *Netlink) Services() ([]Service, error) {
	req := n.request(cmdGetService, unix.NLM_F_DUMP)
	req.AddData(nl.NewRtAttr(attrService, nil))
	msgs, err := n.exec(req)
	if err != nil {
		return nil, err
	}
	var out []Service
	for _, m := range msgs {
		if len(m) < nl.SizeofGenlmsg {
			continue
		}
		top, err := nl.ParseRouteAttr(m[nl.SizeofGenlmsg:])
		if err != nil {
			return nil, err
		}
		for _, t := range top {
			if int(t.Attr.Type) != attrService {
				continue
			}
			inner, err := nl.ParseRouteAttr(t.Value)
			if err != nil {
				return nil, err
			}
			s, ok := parseService(inner)
			if ok {
				out = append(out, s)
			}
		}
	}
	return out, nil
}

func parseService(attrs []syscall.NetlinkRouteAttr) (Service, bool) {
	var s Service
	af := uint16(unix.AF_INET)
	for _, a := range attrs {
		switch int(a.Attr.Type) {
		case svcAF:
			af = nl.NativeEndian().Uint16(a.Value)
		case svcProtocol:
			s.Protocol = nl.NativeEndian().Uint16(a.Value)
		case svcAddr:
			if len(a.Value) >= 4 {
				s.Address = net.IP(append([]byte(nil), a.Value[:4]...))
			}
		case svcPort:
			s.Port = binary.BigEndian.Uint16(a.Value)
		case svcSched:
			s.Scheduler = nl.BytesToString(a.Value)
		case svcFlags:
			if len(a.Value) >= 4 {
				s.Flags = nl.NativeEndian().Uint32(a.Value[:4])
			}
		case svcTimeout:
			s.Timeout = nl.NativeEndian().Uint32(a.Value)
		case svcNetmask:
			s.Netmask = nl.NativeEndian().Uint32(a.Value)
		}
	}
	if af != unix.AF_INET || s.Address == nil {
		return s, false // fwmark services and IPv6 are not ours
	}
	return s, true
}

// Destinations dumps the real servers of one service.
func (n *Netlink) Destinations(s Service) ([]Destination, error) {
	req := n.request(cmdGetDest, unix.NLM_F_DUMP)
	sa := serviceAttr(s)
	if sa == nil {
		return nil, errors.New("IPv4 only")
	}
	req.AddData(sa)
	msgs, err := n.exec(req)
	if err != nil {
		return nil, err
	}
	var out []Destination
	for _, m := range msgs {
		if len(m) < nl.SizeofGenlmsg {
			continue
		}
		top, err := nl.ParseRouteAttr(m[nl.SizeofGenlmsg:])
		if err != nil {
			return nil, err
		}
		for _, t := range top {
			if int(t.Attr.Type) != attrDest {
				continue
			}
			inner, err := nl.ParseRouteAttr(t.Value)
			if err != nil {
				return nil, err
			}
			var d Destination
			for _, a := range inner {
				switch int(a.Attr.Type) {
				case destAddr:
					if len(a.Value) >= 4 {
						d.Address = net.IP(append([]byte(nil), a.Value[:4]...))
					}
				case destPort:
					d.Port = binary.BigEndian.Uint16(a.Value)
				case destFwd:
					d.Forward = nl.NativeEndian().Uint32(a.Value)
				case destWeight:
					d.Weight = int(nl.NativeEndian().Uint32(a.Value))
				case destActive:
					d.ActiveConns = nl.NativeEndian().Uint32(a.Value)
				case destInact:
					d.InactConns = nl.NativeEndian().Uint32(a.Value)
				case destTunType:
					if len(a.Value) >= 1 {
						d.TunType = a.Value[0]
					}
				}
			}
			if d.Address != nil {
				out = append(out, d)
			}
		}
	}
	return out, nil
}

// StartDaemon: ipvsadm --start-daemon master|backup --mcast-interface iface --syncid id.
func (n *Netlink) StartDaemon(state uint32, iface string, syncID uint32) error {
	req := n.request(cmdNewDaemon, 0)
	a := nl.NewRtAttr(attrDaemon, nil)
	nl.NewRtAttrChild(a, daemonState, nl.Uint32Attr(state))
	nl.NewRtAttrChild(a, daemonMcastIfn, nl.ZeroTerminated(iface))
	nl.NewRtAttrChild(a, daemonSyncID, nl.Uint32Attr(syncID))
	req.AddData(a)
	_, err := n.exec(req)
	if err != nil && errors.Is(err, unix.EEXIST) {
		return nil // already running in that state
	}
	return err
}

// StopDaemon: ipvsadm --stop-daemon master|backup.
func (n *Netlink) StopDaemon(state uint32) error {
	req := n.request(cmdDelDaemon, 0)
	a := nl.NewRtAttr(attrDaemon, nil)
	nl.NewRtAttrChild(a, daemonState, nl.Uint32Attr(state))
	req.AddData(a)
	_, err := n.exec(req)
	if err != nil && (errors.Is(err, unix.ESRCH) || errors.Is(err, unix.ENOENT)) {
		return nil
	}
	return err
}

// Stats reads /proc/net/ip_vs_stats (totals and per-second rates).
func (n *Netlink) Stats() (Stats, error) {
	return ReadStats("/proc/net/ip_vs_stats")
}

// ReadStats parses an ip_vs_stats-shaped file.
func ReadStats(path string) (Stats, error) { return readStatsFile(path) }

// ConnMemory reports IPVS connection-table memory: entries and the slab
// object size of ip_vs_conn (bytes), from /proc/slabinfo (root).
func ConnMemory() (entries int, objSize int, err error) {
	f, err := os.Open("/proc/slabinfo")
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) > 3 && fields[0] == "ip_vs_conn" {
			entries, _ = strconv.Atoi(fields[1])
			objSize, _ = strconv.Atoi(fields[3])
			return entries, objSize, nil
		}
	}
	return 0, 0, errors.New("ip_vs_conn not in slabinfo")
}
