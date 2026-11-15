package ipvs

import (
	"bufio"
	"os"
	"strconv"
	"strings"
)

// readStatsFile parses /proc/net/ip_vs_stats: a header, then a totals row
// and a per-second rates row, hex fields.
func readStatsFile(path string) (Stats, error) {
	f, err := os.Open(path)
	if err != nil {
		return Stats{}, err
	}
	defer f.Close()
	var st Stats
	var rows [][]uint64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) != 5 {
			continue
		}
		var row []uint64
		for _, fld := range fields {
			v, err := strconv.ParseUint(fld, 16, 64)
			if err != nil {
				row = nil
				break
			}
			row = append(row, v)
		}
		if row != nil {
			rows = append(rows, row)
		}
	}
	if len(rows) >= 1 {
		st.Conns, st.PacketsIn, st.PacketsOut, st.BytesIn, st.BytesOut = rows[0][0], rows[0][1], rows[0][2], rows[0][3], rows[0][4]
	}
	if len(rows) >= 2 {
		st.CPS, st.PPSIn, st.PPSOut, st.BPSIn, st.BPSOut = rows[1][0], rows[1][1], rows[1][2], rows[1][3], rows[1][4]
	}
	return st, nil
}
