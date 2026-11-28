package main

// Swap returns a copy of tiles with positions i and j exchanged.
func Swap(tiles []int, i, j int) []int {
	out := append([]int(nil), tiles...)
	out[i], out[j] = out[j], out[i]
	return out
}
