package main

import "testing"

func TestSwapExchangesTwoTiles(t *testing.T) {
	got := Swap([]int{1, 2, 3}, 0, 2)
	if got[0] != 3 || got[2] != 1 {
		t.Errorf("Swap = %v, want [3 2 1]", got)
	}
}
