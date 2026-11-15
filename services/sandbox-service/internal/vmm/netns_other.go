//go:build !linux

package vmm

import "errors"

var errLinuxOnly = errors.New("network namespaces are linux-only")

func netnsName(fcid string) string                { return "bv-" + fcid }
func netnsPath(name string) string                { return "/run/netns/" + name }
func createNetns(string) error                    { return errLinuxOnly }
func deleteNetns(string) error                    { return nil }
func ensureReplicaLink(string, string, int) error { return errLinuxOnly }
func hostLinkExists(string) bool                  { return false }
