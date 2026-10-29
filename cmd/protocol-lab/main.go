//go:build protocolaudit

// A local-only experiment, deliberately excluded from production builds.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
)

func main() {
	if err := run(); err != nil {
		fields := map[string]any{"success": false, "error_type": fmt.Sprintf("%T", err)}
		if strings.HasPrefix(err.Error(), "PCMgr ") || strings.HasPrefix(err.Error(), "invalid PCMgr ") {
			fields["stage_error"] = err.Error()
		}
		var manual *protocol.ManualAuthRejectedError
		var buffer *pcmgr.BufferRejectedError
		var short *protocol.ShortlinkRejectedError
		if errors.As(err, &manual) {
			fields["manualauth_code"] = manual.Code
		}
		if errors.As(err, &buffer) {
			fields["pcmgr_code"] = buffer.Code
		}
		if errors.As(err, &short) {
			fields["shortlink_code"] = short.Code
		}
		_ = json.NewEncoder(os.Stdout).Encode(fields)
		os.Exit(1)
	}
}

func run() error {
	provider := flag.String("provider", "", "required: yyb or pcmgr")
	root := flag.String("state-root", "", "absolute private lab directory; each provider has a separate subdirectory")
	inputPath := flag.String("ticket-file", "", "fresh YYB base64 ticket or PCMgr native protobuf response; omitted to reuse a session")
	pcProfile := flag.String("pcmgr-profile", "", "private PCMgr profile for the independent Go transport")
	pcRenew := flag.Bool("renew-pcmgr", false, "rotate the isolated PCMgr profile and persist before fetching a ticket")
	flag.Parse()
	if (*provider != "yyb" && *provider != "pcmgr") || !filepath.IsAbs(*root) || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "requires --provider yyb|pcmgr --state-root ABSOLUTE_PATH [--ticket-file ABSOLUTE_PATH]")
		return errors.New("invalid local lab arguments")
	}
	dir := filepath.Join(*root, *provider)
	statePath := filepath.Join(dir, "session.json")
	var input, saved []byte
	var err error
	if *pcProfile != "" {
		if *provider != "pcmgr" || !filepath.IsAbs(*pcProfile) || *inputPath != "" {
			return errors.New("invalid PCMgr profile arguments")
		}
		raw, readErr := os.ReadFile(*pcProfile)
		if readErr != nil {
			return errors.New("private profile unavailable")
		}
		var creds pcmgr.Credentials
		if json.Unmarshal(raw, &creds) != nil {
			return errors.New("invalid private profile")
		}
		hc := &http.Client{Timeout: 20 * time.Second}
		if *pcRenew {
			creds, err = pcmgr.Renew(context.Background(), hc, creds)
			if err != nil {
				return err
			}
			updated, _ := json.Marshal(creds)
			file, err := os.CreateTemp(filepath.Dir(*pcProfile), ".renew-*")
			if err != nil {
				return err
			}
			defer os.Remove(file.Name())
			_, writeErr := file.Write(updated)
			closeErr := file.Close()
			if writeErr != nil || closeErr != nil || os.Rename(file.Name(), *pcProfile) != nil {
				return errors.New("rotated profile save failed")
			}
		}
		buffer, fetchErr := pcmgr.FetchBuffer(context.Background(), hc, creds)
		if fetchErr != nil {
			return fetchErr
		}
		input = pcmgr.PBBytes(nil, 3, []byte(buffer))
	} else if *inputPath != "" {
		if !filepath.IsAbs(*inputPath) {
			return errors.New("ticket file must be absolute")
		}
		input, err = os.ReadFile(*inputPath)
	} else {
		saved, err = os.ReadFile(statePath)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "private lab input unavailable")
		return errors.New("private lab input unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	result, state, err := protocol.ProbeLocalCode(ctx, *provider, input, saved)
	if err != nil {
		return err
	}
	if len(input) > 0 {
		if err = os.MkdirAll(dir, 0700); err != nil {
			return errors.New("private session directory unavailable")
		}
		file, err := os.CreateTemp(dir, ".session-*")
		if err != nil {
			return errors.New("private session save unavailable")
		}
		defer os.Remove(file.Name())
		_, writeErr := file.Write(state)
		closeErr := file.Close()
		if writeErr != nil || closeErr != nil || os.Rename(file.Name(), statePath) != nil {
			return errors.New("private session save failed")
		}
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}
