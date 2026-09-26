package webrtc

import "testing"

func TestNormalizeCandidate(t *testing.T) {
	cases := []struct {
		input, want string
		ok          bool
	}{
		{"candidate:1 1 udp 123 192.0.2.1 5000 typ host generation 0 network-cost 10", "candidate:1 1 udp 123 192.0.2.1 5000 typ host", true},
		{"candidate:2 1 udp 123 198.51.100.3 5001 typ srflx raddr 192.0.2.1 rport 5000", "candidate:2 1 udp 123 198.51.100.3 5001 typ srflx", true},
		{"candidate:3 1 udp 123 127.0.0.1 5000 typ host", "", false},
		{"candidate:4 1 udp 123 2001:db8::1 5000 typ host", "", false},
	}
	for _, tc := range cases {
		got, ok := normalizeCandidate(tc.input)
		if got != tc.want || ok != tc.ok {
			t.Errorf("normalizeCandidate(%q) = %q, %v; want %q, %v", tc.input, got, ok, tc.want, tc.ok)
		}
	}
}

func TestVideoSize(t *testing.T) {
	if got := videoSize("1280x720"); got != "medium" {
		t.Fatalf("720p size = %q", got)
	}
	if got := videoSize("3840x2160"); got != "1920x1080" {
		t.Fatalf("4K size = %q", got)
	}
}
