//go:build !linux

package vmm

func copySparseFast(src, dst string) error { return copySparse(src, dst) }
