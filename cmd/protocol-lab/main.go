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
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
)

func main() {
	if err := run(); err != nil {
		_ = json.NewEncoder(os.Stdout).Encode(failureFields(err))
		os.Exit(1)
	}
}

func failureFields(err error) map[string]any {
	fields := map[string]any{"success": false, "error_type": fmt.Sprintf("%T", err)}
	var manual *protocol.ManualAuthRejectedError
	var native *protocol.AutoAuthRejectedError
	var buffer *pcmgr.BufferRejectedError
	var short *protocol.ShortlinkRejectedError
	if errors.As(err, &manual) {
		fields["manualauth_code"] = manual.Code
	}
	if errors.As(err, &native) {
		fields["native_autoauth_stage"] = native.Stage
		fields["native_autoauth_code"] = native.Code
	}
	if errors.As(err, &buffer) {
		fields["pcmgr_code"] = buffer.Code
	}
	if errors.As(err, &short) {
		fields["shortlink_code"] = short.Code
	}
	return fields
}

func savePrivateSession(dir, statePath string, state []byte) error {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return errors.New("private session directory unavailable")
	}
	file, err := os.CreateTemp(dir, ".session-*")
	if err != nil {
		return errors.New("private session save unavailable")
	}
	defer os.Remove(file.Name())
	_, writeErr := file.Write(state)
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil || os.Rename(file.Name(), statePath) != nil {
		return errors.New("private session save failed")
	}
	return nil
}

func recordNativeIntent(dir string) error {
	file, err := os.OpenFile(filepath.Join(dir, "native-autoauth-intent.json"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return errors.New("native autoauth already attempted or intent unavailable")
	}
	_, writeErr := fmt.Fprintf(file, "{\"attempted_at\":%d}\n", time.Now().Unix())
	syncErr := file.Sync()
	closeErr := file.Close()
	return errors.Join(writeErr, syncErr, closeErr)
}

func recordNativeOutcome(dir string, outcome any) error {
	file, err := os.OpenFile(filepath.Join(dir, "native-autoauth-result.json"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return errors.New("native outcome already recorded or unavailable")
	}
	writeErr := json.NewEncoder(file).Encode(outcome)
	syncErr := file.Sync()
	closeErr := file.Close()
	return errors.Join(writeErr, syncErr, closeErr)
}

func run() error {
	provider := flag.String("provider", "", "required: yyb, pcmgr, gameassist or ldplayer")
	root := flag.String("state-root", "", "absolute private lab directory; each provider has a separate subdirectory")
	inputPath := flag.String("ticket-file", "", "fresh YYB base64 ticket or PCMgr native protobuf response; omitted to reuse a session")
	pcProfile := flag.String("pcmgr-profile", "", "private PCMgr profile for the independent Go transport")
	pcRenew := flag.Bool("renew-pcmgr", false, "rotate the isolated PCMgr profile and persist before fetching a ticket")
	nativeAutoAuth := flag.Bool("native-autoauth", false, "rotate the isolated saved ILink session once; records intent before sending")
	flag.Parse()
	if (*provider != "yyb" && *provider != "pcmgr" && *provider != "gameassist" && *provider != "ldplayer") || !filepath.IsAbs(*root) || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "requires --provider yyb|pcmgr|gameassist|ldplayer --state-root ABSOLUTE_PATH [--ticket-file ABSOLUTE_PATH]")
		return errors.New("invalid local lab arguments")
	}
	dir := filepath.Join(*root, *provider)
	statePath := filepath.Join(dir, "session.json")
	if *nativeAutoAuth && (*inputPath != "" || *pcProfile != "" || *pcRenew) {
		return errors.New("native autoauth requires only a saved isolated session")
	}
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
	var result protocol.LabResult
	var state []byte
	if *nativeAutoAuth {
		result, err = protocol.ProbeLocalNativeAutoAuth(ctx, *provider, saved,
			func() error { return recordNativeIntent(dir) },
			func(raw []byte) error { return savePrivateSession(dir, statePath, raw) })
		outcome := any(result)
		if err != nil {
			outcome = failureFields(err)
		}
		if saveErr := recordNativeOutcome(dir, outcome); saveErr != nil {
			return errors.Join(err, saveErr)
		}
	} else {
		result, state, err = protocol.ProbeLocalCode(ctx, *provider, input, saved)
	}
	if len(state) > 0 && len(input) > 0 {
		if saveErr := savePrivateSession(dir, statePath, state); saveErr != nil {
			return errors.Join(err, saveErr)
		}
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}
