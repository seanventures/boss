package main

import (
	"os"

	"gw/internal/lint"
)

func main() {
	lint.LoadDoc(os.Args[2])
}
