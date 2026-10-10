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
