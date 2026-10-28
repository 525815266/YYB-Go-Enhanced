package protocol

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestCredentialRejectionsRedactSubmittedSecrets(t *testing.T) {
	creds := LoginBufferCredentials{OpenID: "private-account", AccessToken: "private-access", RefreshToken: "private-refresh"}
	for _, operation := range []string{"refresh", "buffer"} {
		t.Run(operation, func(t *testing.T) {
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
				return credentialTestResponse(`{"code":-101,"msg":"error 42007 token private-access refresh private-refresh openid private-account"}`), nil
			})}, time.Second)
			var err error
			if operation == "refresh" {
				_, err = client.RefreshCredentials(context.Background(), creds)
				var rejected *RefreshRejectedError
				if !errors.As(err, &rejected) || rejected.Code != -101 {
					t.Fatal("lost refresh rejection type/code")
				}
			} else {
				_, err = client.FetchLoginBuffer(context.Background(), creds)
				var rejected *LoginBufferRejectedError
				if !errors.As(err, &rejected) || rejected.Code != -101 {
					t.Fatal("lost buffer rejection type/code")
				}
			}
			if err == nil || !strings.Contains(err.Error(), "42007") {
				t.Fatal("lost upstream diagnostic code")
			}
			for _, secret := range []string{creds.OpenID, creds.AccessToken, creds.RefreshToken} {
				if strings.Contains(err.Error(), secret) {
					t.Fatal("upstream credential echo leaked into error")
				}
			}
		})
	}
}

func TestMalformedCredentialResponsesDoNotEchoBody(t *testing.T) {
	for _, status := range []int{http.StatusBadGateway, http.StatusOK} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
				response := credentialTestResponse(`{"echo":"private-token-body"} trailing-invalid-json`)
				response.StatusCode = status
				return response, nil
			})}, time.Second)
			_, err := client.FetchLoginBuffer(context.Background(), LoginBufferCredentials{})
			if err == nil || strings.Contains(err.Error(), "private-token-body") {
				t.Fatal("raw HTTP/JSON error response leaked")
			}
			var business *LoginBufferRejectedError
			if errors.As(err, &business) {
				t.Fatal("transport/JSON failure converted into credential rejection")
			}
		})
	}
}
