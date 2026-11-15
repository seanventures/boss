//go:build !linux

package ipvs

import "errors"

// Netlink is Linux-only; on other systems Open fails and the Fake is the
// only backend (tests).
type Netlink struct{}

// Open always fails off Linux.
func Open() (*Netlink, error) { return nil, errors.New("ipvs: linux only") }

func (n *Netlink) Close()                       {}
func (n *Netlink) Services() ([]Service, error) { return nil, errors.New("ipvs: linux only") }
func (n *Netlink) Destinations(Service) ([]Destination, error) {
	return nil, errors.New("ipvs: linux only")
}
func (n *Netlink) AddService(Service) error                  { return errors.New("ipvs: linux only") }
func (n *Netlink) UpdateService(Service) error               { return errors.New("ipvs: linux only") }
func (n *Netlink) DelService(Service) error                  { return errors.New("ipvs: linux only") }
func (n *Netlink) AddDestination(Service, Destination) error { return errors.New("ipvs: linux only") }
func (n *Netlink) UpdateDestination(Service, Destination) error {
	return errors.New("ipvs: linux only")
}
func (n *Netlink) DelDestination(Service, Destination) error { return errors.New("ipvs: linux only") }
func (n *Netlink) StartDaemon(uint32, string, uint32) error  { return errors.New("ipvs: linux only") }
func (n *Netlink) StopDaemon(uint32) error                   { return errors.New("ipvs: linux only") }
func (n *Netlink) Stats() (Stats, error)                     { return Stats{}, errors.New("ipvs: linux only") }

// ReadStats parses an ip_vs_stats-shaped file (portable; tests).
func ReadStats(path string) (Stats, error) { return readStatsFile(path) }

// ConnMemory is Linux-only.
func ConnMemory() (int, int, error) { return 0, 0, errors.New("ipvs: linux only") }
