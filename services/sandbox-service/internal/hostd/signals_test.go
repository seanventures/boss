package hostd

import "syscall"

func killPID(pid int) { _ = syscall.Kill(pid, syscall.SIGKILL) }
func stopPID(pid int) { _ = syscall.Kill(pid, syscall.SIGSTOP) }
