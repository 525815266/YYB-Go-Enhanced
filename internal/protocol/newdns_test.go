package protocol

import (
	"context"
	"reflect"
	"testing"
	"time"
)

func TestOrderLonglinkTargetsRetainsPortsAndResolverOrder(t *testing.T) {
	input := []Target{
		{IP: "203.0.113.20", Port: 8080}, {IP: "203.0.113.10", Port: 8080},
		{IP: "203.0.113.20", Port: 443}, {IP: "203.0.113.10", Port: 443},
		{IP: "203.0.113.20", Port: 443}, {IP: "203.0.113.20", Port: 80},
		{IP: "203.0.113.20", Port: 5000}, {IP: "203.0.113.20", Port: 8443},
	}
	want := []Target{
		{IP: "203.0.113.20", Port: 443}, {IP: "203.0.113.10", Port: 443},
		{IP: "203.0.113.20", Port: 8080}, {IP: "203.0.113.10", Port: 8080},
		{IP: "203.0.113.20", Port: 80}, {IP: "203.0.113.20", Port: 5000},
		{IP: "203.0.113.20", Port: 8443},
	}
	for _, limit := range []int{0, 6} {
		expected := want
		if limit > 0 {
			expected = want[:limit]
		}
		if got := orderLonglinkTargets(input, limit); !reflect.DeepEqual(got, expected) {
			t.Fatalf("limit %d: got %v, want %v", limit, got, expected)
		}
	}
}

func TestGetLonglinkTargetsFallsBackToOfficialHostname(t *testing.T) {
	dnsCache.Lock()
	dnsCache.entries = map[string]dnsCacheEntry{
		"0|Windows": {
			ExpiresAt: time.Now().Add(time.Minute),
			Parsed:    map[string]dnsDomain{},
		},
	}
	dnsCache.Unlock()
	t.Cleanup(clearDNSCache)

	targets, err := getLonglinkTargets(context.Background(), time.Second, time.Minute)
	if err != nil {
		t.Fatalf("getLonglinkTargets() error = %v", err)
	}
	if len(targets) != 1 || targets[0].IP != longlinkDomain || targets[0].Port != 443 {
		t.Fatalf("getLonglinkTargets() = %#v, want %s:443", targets, longlinkDomain)
	}
}

func TestGetLonglinkTargetsUsesHTTPDNSCandidates(t *testing.T) {
	dnsCache.Lock()
	dnsCache.entries = map[string]dnsCacheEntry{
		"0|Windows": {
			ExpiresAt: time.Now().Add(time.Minute),
			Parsed: map[string]dnsDomain{
				longlinkDomain: {
					IPs:       []string{"203.0.113.10"},
					Protocols: map[string][]int{protoMMTLS: []int{8080}},
				},
			},
		},
	}
	dnsCache.Unlock()
	t.Cleanup(clearDNSCache)

	targets, err := getLonglinkTargets(context.Background(), time.Second, time.Minute)
	if err != nil {
		t.Fatalf("getLonglinkTargets() error = %v", err)
	}
	if len(targets) != 1 || targets[0].IP != "203.0.113.10" || targets[0].Port != 8080 {
		t.Fatalf("getLonglinkTargets() = %#v", targets)
	}
}

func clearDNSCache() {
	dnsCache.Lock()
	dnsCache.entries = map[string]dnsCacheEntry{}
	dnsCache.Unlock()
}

func TestLonglinkLoginTargetsReservesHostnameWithFullHTTPDNSList(t *testing.T) {
	input := []Target{
		{IP: "203.0.113.10", Port: 8080}, {IP: "203.0.113.10", Port: 443},
		{IP: "203.0.113.11", Port: 443}, {IP: "203.0.113.12", Port: 443},
		{IP: "203.0.113.13", Port: 443}, {IP: "203.0.113.14", Port: 443},
		{IP: "203.0.113.15", Port: 443}, {IP: "203.0.113.16", Port: 443},
	}
	want := []Target{
		{IP: "203.0.113.10", Port: 443}, {IP: "203.0.113.11", Port: 443},
		{IP: "203.0.113.12", Port: 443}, {IP: "203.0.113.13", Port: 443},
		{IP: "203.0.113.14", Port: 443}, {IP: longlinkDomain, Port: 443},
	}
	if got := longlinkLoginTargets(input, 6); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestLonglinkLoginTargetsKeepsFallbackOnceAndSupportsSmallLists(t *testing.T) {
	fallback := Target{IP: longlinkDomain, Port: 443}
	ip := Target{IP: "203.0.113.10", Port: 8080}
	for _, tc := range []struct {
		name  string
		input []Target
		max   int
		want  []Target
	}{
		{"empty", nil, 6, []Target{fallback}},
		{"resolver fallback", []Target{fallback}, 6, []Target{fallback}},
		{"duplicate", []Target{fallback, ip, fallback, ip}, 6, []Target{ip, fallback}},
		{"one slot", []Target{ip, fallback}, 1, []Target{fallback}},
		{"unlimited", []Target{ip}, 0, []Target{ip, fallback}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := longlinkLoginTargets(tc.input, tc.max); !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
		})
	}
}
