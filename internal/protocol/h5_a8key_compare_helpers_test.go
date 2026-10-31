//go:build issue75probe

package protocol

import (
	"encoding/binary"
	"encoding/hex"
	"html"
	"net/url"
	"regexp"
	"strings"
	"testing"

	xhtml "golang.org/x/net/html"
)

// Reuse the archived, independently verified A8Key vector and confirmation
// parser. Live orchestration and strict callback checks are in the H5 audit.
func issue75AuthorizeReply(page []byte, origin *url.URL, scope string) (*url.URL, []string) {
	doc, err := xhtml.Parse(strings.NewReader(string(page)))
	if err != nil {
		return nil, nil
	}
	fields := map[string]string{}
	var names []string
	var walk func(*xhtml.Node)
	walk = func(n *xhtml.Node) {
		if n.Type == xhtml.ElementNode && n.Data == "input" {
			name, value := "", ""
			for _, a := range n.Attr {
				if a.Key == "name" {
					name = a.Val
				}
				if a.Key == "value" {
					value = a.Val
				}
			}
			if name != "" {
				fields[name] = value
				names = append(names, name)
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(doc)
	if scope != "snsapi_userinfo" && scope != "snsapi_base" {
		return nil, names
	}
	q := url.Values{"allow": {"1"}, scope: {"on"}}
	for _, k := range []string{"uuid", "uin", "key", "pass_ticket", "version"} {
		q.Set(k, fields[k])
	}
	if q.Get("version") == "" {
		m := regexp.MustCompile(`(?is)&version="\s*\+\s*encodeURIComponent\("([^"]+)"\)`).FindSubmatch(page)
		if len(m) == 2 {
			q.Set("version", html.UnescapeString(string(m[1])))
		}
	}
	// Some server pages supply a complete confirmation URL instead of inputs.
	m := regexp.MustCompile(`(?is)var\s+authUrl\s*=\s*"([^"]+)"`).FindSubmatch(page)
	if len(m) == 2 {
		u, e := url.Parse(html.UnescapeString(string(m[1])))
		if e == nil {
			u = origin.ResolveReference(u)
			if u.Scheme == "https" && u.Host == "open.weixin.qq.com" && u.Path == "/connect/oauth2/authorize_reply" && u.User == nil && u.Query().Get("pass_ticket") != "" {
				return u, names
			}
		}
	}
	for _, k := range []string{"uuid", "uin", "key", "pass_ticket"} {
		if q.Get(k) == "" {
			return nil, names
		}
	}
	return &url.URL{Scheme: "https", Host: "open.weixin.qq.com", Path: "/connect/oauth2/authorize_reply", RawQuery: q.Encode()}, names
}

func issue75GetA8Key(base []byte, target string) []byte {
	out := pbLen(1, base)
	out = append(out, pbVar(2, 2)...)
	for _, n := range []int{4, 5, 6} {
		out = append(out, pbLen(n, pbLen(1, nil))...)
	}
	out = append(out, pbLen(7, pbLen(1, []byte(target)))...)
	for _, v := range [][2]uint64{{9, 0}, {10, 1}, {14, 2}, {15, 100}} {
		out = append(out, pbVar(int(v[0]), v[1])...)
	}
	// Existing pbLen/pbVar only encode single-byte tags (fields 1..15).
	out = append(out, 0x80, 0x01, 0, 0x8a, 0x01, 4)
	out = append(out, []byte("WIFI")...)
	out = append(out, 0x90, 0x01, 0, 0x98, 0x01, 0)
	return out
}

func TestIssue75A8KeyOfflineVector(t *testing.T) {
	base, _ := hex.DecodeString("0a0a73657373696f6e6b657910b9601a0364657620ff8d9c98062a0757696e646f77733000")
	want := "0a250a0a73657373696f6e6b657910b9601a0364657620ff8d9c98062a0757696e646f77733000100222020a002a020a0032020a003a1e0a1c68747470733a2f2f6578616d706c652e636f6d2f63616c6c6261636b48005001700278648001008a010457494649900100980100"
	if got := hex.EncodeToString(issue75GetA8Key(base, "https://example.com/callback")); got != want {
		t.Fatalf("wire mismatch: %s", got)
	}
}

func issue75RoutePacket(st AppSession, body []byte, cmd uint32, cgi []byte) ([]byte, error) {
	enc, err := aesGCMEncryptLayout(st.SendKey, nil, lz4AllLiteral(body))
	if err != nil {
		return nil, err
	}
	inner := buildShortlink(cmd, 0, append(sessionWpkgHead(st.UIN, st.F9, st.DeviceID), enc...), defaultVer)
	env := []byte{}
	var b [4]byte
	for _, s := range [][]byte{cgi, transferHost} {
		binary.BigEndian.PutUint16(b[:2], uint16(len(s)))
		env = append(env, b[:2]...)
		env = append(env, s...)
	}
	binary.BigEndian.PutUint32(b[:], uint32(len(inner)))
	env = append(env, b[:]...)
	env = append(env, inner...)
	binary.BigEndian.PutUint32(b[:], uint32(len(env)))
	return append(b[:], env...), nil
}
