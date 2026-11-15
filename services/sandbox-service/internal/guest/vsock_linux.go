//go:build linux

package guest

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"time"

	"golang.org/x/sys/unix"
)

// VsockTransport is the in-VM transport: control on vsock port 5000, relays
// dial the host (CID 2) on the route's port.
type VsockTransport struct{}

func (VsockTransport) ListenControl() (net.Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: PortControl}); err != nil {
		unix.Close(fd)
		return nil, err
	}
	if err := unix.Listen(fd, 64); err != nil {
		unix.Close(fd)
		return nil, err
	}
	return &vsockListener{fd: fd, port: PortControl}, nil
}

func (VsockTransport) DialHost(port int) (net.Conn, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	tv := unix.NsecToTimeval(int64(2 * time.Second))
	_ = unix.SetsockoptTimeval(fd, unix.SOL_SOCKET, unix.SO_SNDTIMEO, &tv)
	if err := unix.Connect(fd, &unix.SockaddrVM{CID: HostCID, Port: uint32(port)}); err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("vsock connect %d: %w", port, err)
	}
	return newVsockConn(fd, port)
}

type vsockListener struct {
	fd   int
	port int
}

func (l *vsockListener) Accept() (net.Conn, error) {
	nfd, _, err := unix.Accept4(l.fd, unix.SOCK_CLOEXEC)
	if err != nil {
		return nil, err
	}
	return newVsockConn(nfd, l.port)
}

func (l *vsockListener) Close() error {
	_ = unix.Shutdown(l.fd, unix.SHUT_RDWR) // wakes a blocked accept
	return unix.Close(l.fd)
}

func (l *vsockListener) Addr() net.Addr { return vsockAddr(l.port) }

type vsockAddr int

func (a vsockAddr) Network() string { return "vsock" }
func (a vsockAddr) String() string  { return "vsock:" + strconv.Itoa(int(a)) }

type vsockConn struct {
	*os.File
	port int
}

func newVsockConn(fd, port int) (net.Conn, error) {
	if err := unix.SetNonblock(fd, true); err != nil {
		unix.Close(fd)
		return nil, err
	}
	return &vsockConn{File: os.NewFile(uintptr(fd), "vsock"), port: port}, nil
}

func (c *vsockConn) LocalAddr() net.Addr  { return vsockAddr(c.port) }
func (c *vsockConn) RemoteAddr() net.Addr { return vsockAddr(c.port) }
func (c *vsockConn) CloseWrite() error {
	rc, err := c.File.SyscallConn()
	if err != nil {
		return err
	}
	var serr error
	if err := rc.Control(func(fd uintptr) { serr = unix.Shutdown(int(fd), unix.SHUT_WR) }); err != nil {
		return err
	}
	return serr
}
