package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"yyb_go/internal/httpapi"
)

func main() {
	host := flag.String("host", "127.0.0.1", "listen host")
	port := flag.Int("port", 8000, "listen port")
	resourceRoot := flag.String("resource-root", filepath.Join(".", "resource"), "runtime resource directory")
	dbFilename := flag.String("db", httpapi.DefaultDBFilename, "SQLite database filename under resource/db")
	tcpProxy := flag.String("tcp-proxy", "", "optional TCP proxy: socks5://host:port or http-connect://host:port")
	keepAliveInterval := flag.Duration("keepalive-interval", time.Minute, "account keepalive check interval; 0 disables")
	keepAliveAhead := flag.Duration("keepalive-ahead", 45*time.Minute, "refresh credentials this long before expiry")
	flag.Parse()
	if dnsServers, err := configureDNS(os.Getenv("YYB_DNS_SERVERS")); err != nil {
		log.Fatalf("configure DNS: %v", err)
	} else if len(dnsServers) > 0 {
		log.Printf("using configured DNS servers: %s", strings.Join(dnsServers, ", "))
	}

	panelType := getEnvWithFallback("PANEL_TYPE", "YYB_PANEL_TYPE", "QL_TYPE")
	if panelType == "" {
		panelType = "qinglong"
	}
	panelType = strings.ToLower(strings.TrimSpace(panelType))

	var panelURL string
	if panelType == "arcadia" {
		panelURL = getEnvWithFallback("ARCADIA_URL")
		if panelURL == "" {
			panelURL = "http://arcadia:5678"
		}
	} else if panelType == "daidai" {
		daidaiURL := getEnvWithFallback("DAIDAI_URL")
		qlURL := getEnvWithFallback("QL_URL")
		if daidaiURL != "" {
			panelURL = daidaiURL
		} else if qlURL != "" && qlURL != "http://qinglong:5700" {
			panelURL = qlURL
		} else {
			panelURL = "http://daidai-panel:5700"
		}
	} else {
		qlURL := getEnvWithFallback("QL_URL")
		daidaiURL := getEnvWithFallback("DAIDAI_URL")
		if qlURL != "" {
			panelURL = qlURL
		} else if daidaiURL != "" {
			panelURL = daidaiURL
		} else {
			panelURL = "http://qinglong:5700"
		}
	}

	var clientID, clientSecret string
	if panelType == "arcadia" {
		clientID = "api-token"
		clientSecret = getEnvWithFallback("ARCADIA_TOKEN")
	} else if panelType == "daidai" {
		clientID = getEnvWithFallback("DAIDAI_APP_KEY", "QL_CLIENT_ID")
		clientSecret = getEnvWithFallback("DAIDAI_APP_SECRET", "QL_CLIENT_SECRET")
	} else {
		clientID = getEnvWithFallback("QL_CLIENT_ID", "DAIDAI_APP_KEY")
		clientSecret = getEnvWithFallback("QL_CLIENT_SECRET", "DAIDAI_APP_SECRET")
	}

	legacyAuthDSN := os.Getenv("YYB_AUTH_MYSQL_DSN")
	authDriver := strings.ToLower(strings.TrimSpace(os.Getenv("YYB_AUTH_DRIVER")))
	if authDriver == "" {
		if legacyAuthDSN != "" {
			authDriver = "mysql"
		} else {
			authDriver = "sqlite"
		}
	}

	cfg := httpapi.Config{
		ResourceRoot:      *resourceRoot,
		DBFilename:        *dbFilename,
		TCPProxy:          *tcpProxy,
		SessionTTL:        30 * time.Minute,
		RequestTimeout:    8 * time.Second,
		AvatarTimeout:     10 * time.Second,
		ScanTimeout:       180 * time.Second,
		QRSessionTTL:      5 * time.Minute,
		KeepAliveInterval: *keepAliveInterval,
		KeepAliveAhead:    *keepAliveAhead,
		QingLongType:      panelType,
		QingLongURL:       panelURL,
		QingLongClientID:  clientID,
		QingLongSecret:    clientSecret,
		QingLongServer:    os.Getenv("YYB_QINGLONG_SERVER"),
		QingLongRepo:      os.Getenv("YYB_QINGLONG_REPO"),
		AuthDriver:        authDriver,
		AuthDSN:           os.Getenv("YYB_AUTH_DSN"),
		AuthMySQLDSN:      legacyAuthDSN,
		IntegrationToken:  os.Getenv("YYB_INTEGRATION_TOKEN"),
		AdminUser:         getEnvWithFallback("YYB_ADMIN_USER", "YYB_WEB_USER"),
		AdminPassword:     getEnvWithFallback("YYB_ADMIN_PASSWORD", "YYB_WEB_PASSWORD"),
		CookieSecure:      os.Getenv("YYB_COOKIE_SECURE") == "true",
		EnablePCLogin:     strings.EqualFold(strings.TrimSpace(os.Getenv("YYB_ENABLE_PC_LOGIN")), "true"),
	}

	app, err := httpapi.NewApp(cfg)
	if err != nil {
		log.Fatalf("init app: %v", err)
	}
	defer app.Close()

	addr := fmt.Sprintf("%s:%d", *host, *port)
	srv := &http.Server{
		Addr:              addr,
		Handler:           app.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("YYB Go service listening on http://%s", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}

func getEnvWithFallback(keys ...string) string {
	for _, key := range keys {
		if val := os.Getenv(key); val != "" {
			return val
		}
	}
	return ""
}
