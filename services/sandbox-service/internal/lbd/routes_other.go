//go:build !linux

package lbd

import "errors"

// Routes is Linux-only.
type Routes struct{}

func (r *Routes) Apply([]hostapiHost) error { return errors.New("routes: linux only") }

// Sysctls is Linux-only.
func Sysctls(string) error { return errors.New("sysctl: linux only") }

// DefaultIface is Linux-only.
func DefaultIface(string) string { return "" }
