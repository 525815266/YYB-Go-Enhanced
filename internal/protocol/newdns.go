package protocol

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	newdnsHost      = "aedns.weixin.qq.com"
	newdnsBackupIP  = "180.153.202.85"
	newdnsPath      = "/cgi-bin/default/getdns"
	longlinkDomain  = "longcloud.weixin.com"
	shortlinkDomain = "shortcloud.weixin.com"
	protoMMTLS      = "mmtlsovertcp"
	defaultUA       = "MicroMessenger Client"
)

type Target struct {
	IP   string `json:"ip"`
	Port int    `json:"port"`
}

type dnsDomain struct {
	IPs       []string
	Protocols map[string][]int
	Timeout   int
}

type dnsCacheEntry struct {
	ExpiresAt time.Time
	Parsed    map[string]dnsDomain
}

var dnsCache = struct {
	sync.Mutex
	entries map[string]dnsCacheEntry
}{entries: map[string]dnsCacheEntry{}}

func buildDNSQuery(clientVersion int, deviceType string, uin int) string {
	v := url.Values{}
	v.Set("clientversion", strconv.Itoa(clientVersion))
	v.Set("devicetype", deviceType)
	v.Set("uin", strconv.Itoa(uin))
	v.Set("format", "json")
	return v.Encode()
}

func requestNewDNS(ctx context.Context, connectTo string, timeout time.Duration) (int, map[string]any, string, error) {
	host := connectTo
	if host == "" {
		host = newdnsHost
	}
	conn, err := dialDirect(ctx, host, 80, timeout)
	if err != nil {
		return 0, nil, "", err
	}
	defer conn.Close()
	stopCancel := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stopCancel()
	_ = conn.SetDeadline(ioDeadline(ctx, timeout))
	path := newdnsPath + "?" + buildDNSQuery(0, "Windows", 0)
	req := fmt.Sprintf("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: %s\r\nAccept: */*\r\nConnection: close\r\n\r\n", path, newdnsHost, defaultUA)
	if _, err = conn.Write([]byte(req)); err != nil {
		return 0, nil, "", err
	}
	raw, err := io.ReadAll(conn)
	if err != nil {
		return 0, nil, "", err
	}
	head, body := splitHTTP(raw)
	status := 0
	if lines := strings.Split(head, "\n"); len(lines) > 0 {
		parts := strings.Fields(lines[0])
		if len(parts) >= 2 {
			status, _ = strconv.Atoi(parts[1])
		}
	}
	text := string(body)
	var obj map[string]any
	_ = json.Unmarshal(body, &obj)
	return status, obj, text, nil
}

func getDNSParsed(ctx context.Context, timeout, cacheTTL time.Duration, force bool) (map[string]dnsDomain, error) {
	key := "0|Windows"
	now := time.Now()
	if !force && cacheTTL > 0 {
		dnsCache.Lock()
		if ent, ok := dnsCache.entries[key]; ok && ent.ExpiresAt.After(now) {
			dnsCache.Unlock()
			return ent.Parsed, nil
		}
		dnsCache.Unlock()
	}
	var last error
	for _, connectTo := range []string{"", newdnsBackupIP} {
		status, obj, text, err := requestNewDNS(ctx, connectTo, timeout)
		if err != nil {
			last = err
			continue
		}
		if status != 200 || obj == nil {
			last = fmt.Errorf("newdns HTTP %d body=%q", status, text[:min(len(text), 120)])
			continue
		}
		parsed, err := parseDomainList(obj)
		if err != nil {
			last = err
			continue
		}
		if cacheTTL > 0 {
			dnsCache.Lock()
			dnsCache.entries[key] = dnsCacheEntry{ExpiresAt: now.Add(cacheTTL), Parsed: parsed}
			dnsCache.Unlock()
		}
		return parsed, nil
	}
	if last == nil {
		last = fmt.Errorf("newdns request failed")
	}
	return nil, last
}

func parseDomainList(obj map[string]any) (map[string]dnsDomain, error) {
	dnsObj, ok := obj["dns"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("dns missing in response")
	}
	if rc, ok := dnsObj["retcode"].(float64); ok && int(rc) != 0 {
		return nil, fmt.Errorf("newdns retcode=%d", int(rc))
	}
	list, ok := dnsObj["domainlist"].([]any)
	if !ok {
		return nil, fmt.Errorf("domainlist missing in response")
	}
	out := map[string]dnsDomain{}
	for _, item := range list {
		d, ok := item.(map[string]any)
		if !ok {
			continue
		}
		name, _ := d["name"].(string)
		if name == "" {
			continue
		}
		dom := dnsDomain{Protocols: map[string][]int{}}
		if t, ok := d["timeout"].(float64); ok {
			dom.Timeout = int(t)
		}
		if ips, ok := d["iplist"].([]any); ok {
			for _, it := range ips {
				if m, ok := it.(map[string]any); ok {
					if ip, ok := m["ip"].(string); ok && net.ParseIP(ip) != nil {
						dom.IPs = append(dom.IPs, ip)
					}
				}
			}
		}
		if plist, ok := d["protocollist"].([]any); ok {
			for _, it := range plist {
				p, ok := it.(map[string]any)
				if !ok {
					continue
				}
				pname, _ := p["name"].(string)
				if pname == "" {
					continue
				}
				if ports, ok := p["portlist"].([]any); ok {
					for _, pv := range ports {
						if f, ok := pv.(float64); ok {
							dom.Protocols[pname] = append(dom.Protocols[pname], int(f))
						}
					}
				}
			}
		}
		out[name] = dom
	}
	return out, nil
}

func serversFor(parsed map[string]dnsDomain, domain, proto string) []Target {
	info, ok := parsed[domain]
	if !ok {
		return nil
	}
	ports := info.Protocols[proto]
	var out []Target
	for _, ip := range info.IPs {
		for _, p := range ports {
			out = append(out, Target{IP: ip, Port: p})
		}
	}
	return out
}

func getLonglinkTargets(ctx context.Context, timeout, cacheTTL time.Duration) ([]Target, error) {
	parsed, err := getDNSParsed(ctx, timeout, cacheTTL, false)
	if err == nil {
		if targets := serversFor(parsed, longlinkDomain, protoMMTLS); len(targets) > 0 {
			return targets, nil
		}
	}
	// Some networks block or return an empty response from WeChat HTTPDNS while
	// normal DNS and the LongLink 443 port remain reachable. Let net.Dialer (or the
	// configured TCP proxy) resolve the official hostname in that case.
	return []Target{{IP: longlinkDomain, Port: 443}}, nil
}

func getShortlinkTargets(ctx context.Context, timeout, cacheTTL time.Duration) []Target {
	parsed, err := getDNSParsed(ctx, timeout, cacheTTL, false)
	if err != nil {
		return []Target{{IP: "120.241.131.173", Port: 80}}
	}
	targets := serversFor(parsed, shortlinkDomain, "http")
	seen := map[string]bool{}
	var out []Target
	for _, t := range targets {
		if t.Port == 80 && !seen[t.IP] {
			seen[t.IP] = true
			out = append(out, t)
		}
	}
	if len(out) == 0 {
		return []Target{{IP: "120.241.131.173", Port: 80}}
	}
	return out
}

func orderLonglinkTargets(targets []Target, max int) []Target {
	pref := []int{8080, 80, 443, 5000}
	seen := map[string]bool{}
	var out []Target
	for _, p := range pref {
		for _, t := range targets {
			if t.Port == p && !seen[t.IP] {
				seen[t.IP] = true
				out = append(out, t)
			}
		}
	}
	if len(out) == 0 {
		out = append(out, targets...)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].IP < out[j].IP })
	if max > 0 && len(out) > max {
		out = out[:max]
	}
	return out
}

func splitHTTP(raw []byte) (string, []byte) {
	idx := strings.Index(string(raw), "\r\n\r\n")
	if idx < 0 {
		return "", raw
	}
	return string(raw[:idx]), raw[idx+4:]
}
