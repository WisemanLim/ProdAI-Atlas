package cli

import (
	"fmt"
	"strconv"
	"strings"
)

// Ver 는 SemVer 2.0 의 MAJOR.MINOR.PATCH(-pre) 다.
type Ver struct {
	Major, Minor, Patch int
	Pre                 string
}

func ParseVer(s string) (Ver, error) {
	s = strings.TrimPrefix(strings.TrimSpace(s), "v")
	core, pre, _ := strings.Cut(s, "-")
	parts := strings.Split(core, ".")
	for len(parts) < 3 {
		parts = append(parts, "0")
	}
	var v Ver
	var err error
	if v.Major, err = strconv.Atoi(parts[0]); err != nil {
		return v, fmt.Errorf("버전 형식 %q", s)
	}
	if v.Minor, err = strconv.Atoi(parts[1]); err != nil {
		return v, fmt.Errorf("버전 형식 %q", s)
	}
	if v.Patch, err = strconv.Atoi(parts[2]); err != nil {
		return v, fmt.Errorf("버전 형식 %q", s)
	}
	v.Pre = pre
	return v, nil
}

// Cmp 는 a<b 면 -1, 같으면 0, a>b 면 1. 프리릴리스는 정식보다 낮다.
func Cmp(a, b Ver) int {
	for _, d := range [][2]int{{a.Major, b.Major}, {a.Minor, b.Minor}, {a.Patch, b.Patch}} {
		if d[0] != d[1] {
			if d[0] < d[1] {
				return -1
			}
			return 1
		}
	}
	switch {
	case a.Pre == b.Pre:
		return 0
	case a.Pre == "":
		return 1
	case b.Pre == "":
		return -1
	case a.Pre < b.Pre:
		return -1
	}
	return 1
}

// Satisfies 는 ">=0.2.0 <1.0.0" 같은 공백 구분 AND 제약을 검사한다.
func Satisfies(v string, constraint string) (bool, error) {
	ver, err := ParseVer(v)
	if err != nil {
		return false, err
	}
	for _, c := range strings.Fields(constraint) {
		op := strings.TrimRight(c, "0123456789.-v")
		bound, err := ParseVer(strings.TrimPrefix(c, op))
		if err != nil {
			return false, err
		}
		r := Cmp(ver, bound)
		ok := map[string]bool{">=": r >= 0, ">": r > 0, "<=": r <= 0, "<": r < 0, "=": r == 0, "": r == 0}[op]
		if !ok {
			return false, nil
		}
	}
	return true, nil
}
