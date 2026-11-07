package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

const offlineNotifyTimeout = 20 * time.Second

// pushTarget is one notification channel. Token is a secret: it never reaches
// logs, events, error messages or API responses.
type pushTarget struct {
	Channel string
	Token   string
	Topic   string
}

// offlineNotifier tells the operator channel and the account's own push
// channel that an authorization needs a new scan, once per authorization.
type offlineNotifier struct {
	send func(context.Context, pushTarget, string, string) error

	mu       sync.Mutex
	notified map[int64]int64 // account ID -> scan time of the notified authorization
	inflight sync.WaitGroup
}

func newOfflineNotifier(timeout time.Duration) *offlineNotifier {
	client := &http.Client{Timeout: timeout}
	return &offlineNotifier{
		send: func(ctx context.Context, t pushTarget, title, content string) error {
			return postPush(ctx, client, t, title, content)
		},
		notified: map[int64]int64{},
	}
}

// claim reports whether this authorization of the account still needs a
// notice. A re-scan records a new scan time and may notify again.
func (n *offlineNotifier) claim(accountID, scannedAt int64) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	if last, ok := n.notified[accountID]; ok && last == scannedAt {
		return false
	}
	n.notified[accountID] = scannedAt
	return true
}

func (n *offlineNotifier) wait() {
	if n != nil {
		n.inflight.Wait()
	}
}

// notifyOffline is called once an account has transitioned to expired or
// session_only because its authorization was rejected. acc is the snapshot
// taken before the transition; delivery is asynchronous and never blocks
// keepalive or protocol requests.
func (a *App) notifyOffline(acc *store.WechatAccount, status string, cause error) {
	if a.notifier == nil || acc == nil {
		return
	}
	scannedAt := protocol.CredentialsFromMap(acc.Credentials).RefreshTokenObservedAt
	if !a.notifier.claim(acc.ID, scannedAt) {
		return
	}
	snapshot := *acc
	rejections := eventRejections(cause)
	at := time.Now()
	a.notifier.inflight.Add(1)
	go func() {
		defer a.notifier.inflight.Done()
		ctx, cancel := context.WithTimeout(context.Background(), offlineNotifyTimeout)
		defer cancel()
		targets := a.offlineTargets(ctx, &snapshot)
		if len(targets) == 0 {
			return
		}
		title, content := offlineMessage(&snapshot, status, rejections, at)
		fields := eventlog.ForAccount(&snapshot)
		for _, target := range targets {
			if err := a.notifier.send(ctx, target, title, content); err != nil {
				a.events.Emit("account.notify_failed", fields)
				log.Printf("notify: account id=%d channel=%s offline notice failed: %v", snapshot.ID, target.Channel, err)
				continue
			}
			a.events.Emit("account.notified", fields)
		}
	}()
}

func (a *App) offlineTargets(ctx context.Context, acc *store.WechatAccount) []pushTarget {
	var targets []pushTarget
	if a.cfg.NotifyChannel != "" {
		targets = append(targets, pushTarget{Channel: a.cfg.NotifyChannel, Token: a.cfg.NotifyToken, Topic: a.cfg.NotifyTopic})
	}
	if a.cfg.DisableAccountPushNotify || a.qinglong == nil || !a.qinglong.configured() {
		return targets
	}
	target, err := a.accountPushTarget(ctx, acc.ID)
	if err != nil {
		log.Printf("notify: account id=%d push setting unavailable: %v", acc.ID, err)
		return targets
	}
	for _, existing := range targets {
		if existing == target {
			return targets
		}
	}
	if target.Channel != "" {
		targets = append(targets, target)
	}
	return targets
}

// accountPushTarget resolves the push channel configured for the account's
// scripts. The secret stays in the panel environment and is read on demand.
func (a *App) accountPushTarget(ctx context.Context, accountID int64) (pushTarget, error) {
	setting, err := a.db.AccountPushSettingOrDefault(ctx, accountID)
	if err != nil || setting.TokenEnvName == "" || !validPushChannel(setting.Channel) {
		return pushTarget{}, err
	}
	token, err := a.namedEnvValue(ctx, setting.TokenEnvName)
	if err != nil || token == "" {
		return pushTarget{}, err
	}
	target := pushTarget{Channel: setting.Channel, Token: token}
	if setting.TopicEnvName != "" {
		if target.Topic, err = a.namedEnvValue(ctx, setting.TopicEnvName); err != nil {
			return pushTarget{}, err
		}
	}
	return target, nil
}

func validPushChannel(channel string) bool {
	return channel == "serverchan" || channel == "pushplus" || channel == "qywx"
}

func offlineMessage(acc *store.WechatAccount, status string, rejections []eventlog.Rejection, at time.Time) (string, string) {
	name := "#" + strconv.FormatInt(acc.ID, 10)
	for _, label := range []*string{acc.Remark, acc.Nickname} {
		if label != nil && strings.TrimSpace(*label) != "" {
			name += " " + strings.TrimSpace(*label)
			break
		}
	}
	channel := "应用宝"
	switch acc.Credentials["provider"] {
	case "pcmgr":
		channel = "电脑管家"
	case "gameassist":
		channel = "手游助手"
	case "qbbrowser":
		channel = "QQ浏览器"
	case "ldplayer":
		channel = "雷电"
	}
	state := "授权已失效，需要重新扫码"
	if status == store.StatusSessionOnly {
		state = "授权被拒，仅剩缓存会话（需补授权），请尽快重新扫码"
	}
	lines := []string{"账号：" + name, "渠道：" + channel, "状态：" + state, "时间：" + at.Format("2006-01-02 15:04:05")}
	if len(rejections) > 0 {
		parts := make([]string, 0, len(rejections))
		for _, r := range rejections {
			part := fmt.Sprintf("%s %d", r.Stage, r.Code)
			if r.WeChatCode != 0 {
				part += fmt.Sprintf(" / 微信 %d", r.WeChatCode)
			}
			parts = append(parts, part)
		}
		lines = append(lines, "诊断："+strings.Join(parts, "；"))
	}
	lines = append(lines, "请打开 YYB Go 控制台重新扫码。")
	return "YYB Go 账号需要重新扫码", strings.Join(lines, "\n")
}

// Server酱³ SendKeys embed the user number used by their push host.
var serverChanV3Key = regexp.MustCompile(`^sctp(\d+)t`)

func newPushRequest(ctx context.Context, target pushTarget, title, content string) (*http.Request, error) {
	switch target.Channel {
	case "serverchan":
		endpoint := "https://sctapi.ftqq.com/" + url.PathEscape(target.Token) + ".send"
		if m := serverChanV3Key.FindStringSubmatch(target.Token); m != nil {
			endpoint = "https://" + m[1] + ".push.ft07.com/send/" + url.PathEscape(target.Token) + ".send"
		}
		// desp is Markdown: a single newline would join the lines.
		form := url.Values{"title": {title}, "desp": {strings.ReplaceAll(content, "\n", "\n\n")}}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
		if err == nil {
			req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}
		return req, err
	case "pushplus":
		body := map[string]any{"token": target.Token, "title": title, "content": content, "template": "txt"}
		if target.Topic != "" {
			body["topic"] = target.Topic
		}
		return jsonPushRequest(ctx, "https://www.pushplus.plus/send", body)
	case "qywx":
		body := map[string]any{"msgtype": "text", "text": map[string]string{"content": title + "\n\n" + content}}
		return jsonPushRequest(ctx, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="+url.QueryEscape(target.Token), body)
	}
	return nil, errors.New("unsupported notify channel")
}

func jsonPushRequest(ctx context.Context, endpoint string, body map[string]any) (*http.Request, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(raw))
	if err == nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return req, err
}

type pushResponse struct {
	Code    *int `json:"code"`
	Errno   *int `json:"errno"`
	ErrCode *int `json:"errcode"`
}

func pushAccepted(channel string, r pushResponse) bool {
	switch channel {
	case "serverchan":
		return r.Code != nil && *r.Code == 0 || r.Errno != nil && *r.Errno == 0
	case "pushplus":
		return r.Code != nil && *r.Code == 200
	case "qywx":
		return r.ErrCode != nil && *r.ErrCode == 0
	}
	return false
}

func postPush(ctx context.Context, client *http.Client, target pushTarget, title, content string) error {
	req, err := newPushRequest(ctx, target, title, content)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		// url.Error embeds the request URL, which carries the Server酱 and
		// 企业微信 keys. Keep only the transport cause.
		var urlErr *url.Error
		if errors.As(err, &urlErr) {
			err = urlErr.Err
		}
		return fmt.Errorf("send: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var parsed pushResponse
	if json.Unmarshal(raw, &parsed) != nil || !pushAccepted(target.Channel, parsed) {
		// Response bodies are not echoed; they may repeat request fields.
		return errors.New("channel rejected the notice")
	}
	return nil
}
