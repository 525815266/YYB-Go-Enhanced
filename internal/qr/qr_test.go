package qr

import (
	"bytes"
	"context"
	"encoding/base64"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return fn(req) }

func TestValidateQRCodeImage(t *testing.T) {
	// 1x1 transparent PNG. Keeping the fixture inline makes this test independent
	// from the network and from files in the resource directory.
	const pngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
	data, err := base64.StdEncoding.DecodeString(pngBase64)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateQRCodeImage(data); err != nil {
		t.Fatalf("valid PNG rejected: %v", err)
	}
	if err := validateQRCodeImage([]byte("<html>blocked</html>")); err == nil {
		t.Fatal("HTML response was accepted as a QR image")
	}
}

func TestDataURIImageUsesDetectedMimeType(t *testing.T) {
	const pngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
	data, _ := base64.StdEncoding.DecodeString(pngBase64)
	uri := DataURIImage(data)
	if !strings.HasPrefix(uri, "data:image/png;base64,") {
		t.Fatalf("unexpected data URI prefix: %s", uri)
	}
	if !bytes.Contains([]byte(uri), []byte(pngBase64)) {
		t.Fatal("data URI does not contain the encoded image")
	}
}

func TestPollQRCodeAllowsWechatLongPoll(t *testing.T) {
	sess := &Session{
		WXUUID: "test-uuid",
		HTTPClient: &http.Client{
			Timeout: 8 * time.Second,
			Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				deadline, ok := req.Context().Deadline()
				if !ok || time.Until(deadline) < 30*time.Second {
					t.Fatalf("poll request deadline is too short: %v", deadline)
				}
				return &http.Response{
					StatusCode: http.StatusOK,
					Body:       io.NopCloser(strings.NewReader("window.wx_errcode=408;")),
					Header:     make(http.Header),
					Request:    req,
				}, nil
			}),
		},
	}
	result, err := (&Client{}).PollQRCode(context.Background(), sess)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "pending" {
		t.Fatalf("status = %q, want pending", result.Status)
	}
}

func TestPollQRCodeTreatsLongPollDeadlineAsPending(t *testing.T) {
	sess := &Session{
		WXUUID: "test-uuid",
		HTTPClient: &http.Client{
			Timeout: 8 * time.Second,
			Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
				return nil, context.DeadlineExceeded
			}),
		},
	}
	result, err := (&Client{}).PollQRCode(context.Background(), sess)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "pending" {
		t.Fatalf("status = %q, want pending", result.Status)
	}
}
