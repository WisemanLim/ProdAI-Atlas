package cli

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// CLI 로그인 (FR-PLT-IAM-01): OAuth 2.0 Device Authorization Grant (RFC 8628) — 브라우저에서 코드 승인,
// 토큰은 ~/.config/atlas/credentials.json (0600) 에 Manager 별로 저장한다.

type Credentials struct {
	Manager string    `json:"manager"`
	Issuer  string    `json:"issuer"`
	Client  string    `json:"client"`
	Access  string    `json:"access_token"`
	Refresh string    `json:"refresh_token"`
	Expiry  time.Time `json:"expiry"`
}

func CredentialsPath() string {
	if d := os.Getenv("ATLAS_CONFIG_DIR"); d != "" {
		return filepath.Join(d, "credentials.json")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "atlas", "credentials.json")
}

type authConfig struct {
	Mode        string `json:"mode"`
	Issuer      string `json:"issuer"`
	CLIClientID string `json:"cliClientId"`
}

func getJSON(ctx context.Context, c *http.Client, method, u string, form url.Values, out any) (int, error) {
	var body io.Reader
	if form != nil {
		body = strings.NewReader(form.Encode())
	}
	req, _ := http.NewRequestWithContext(ctx, method, u, body)
	if form != nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	res, err := c.Do(req)
	if err != nil {
		return 0, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	return res.StatusCode, json.Unmarshal(raw, out)
}

// DeviceLogin 은 장치 코드를 받아 show 로 안내하고 승인될 때까지 폴링한다.
func DeviceLogin(ctx context.Context, c *http.Client, manager string, show func(verifyURL, code string)) (*Credentials, error) {
	manager = strings.TrimRight(manager, "/")
	var cfg authConfig
	if _, err := getJSON(ctx, c, http.MethodGet, manager+"/api/v1/auth/config", nil, &cfg); err != nil {
		return nil, fmt.Errorf("auth config: %w", err)
	}
	if cfg.Mode != "oidc" {
		return nil, errors.New("이 Manager 는 인증 모드가 아님 (ATLAS_AUTH_MODE=off) — 로그인 불필요")
	}
	var dev struct {
		DeviceCode              string `json:"device_code"`
		UserCode                string `json:"user_code"`
		VerificationURIComplete string `json:"verification_uri_complete"`
		VerificationURI         string `json:"verification_uri"`
		ExpiresIn               int    `json:"expires_in"`
		Interval                int    `json:"interval"`
	}
	if st, err := getJSON(ctx, c, http.MethodPost, cfg.Issuer+"/protocol/openid-connect/auth/device",
		url.Values{"client_id": {cfg.CLIClientID}, "scope": {"openid"}}, &dev); err != nil || st != http.StatusOK {
		return nil, fmt.Errorf("device authorization: %d %v", st, err)
	}
	show(firstNonEmpty(dev.VerificationURIComplete, dev.VerificationURI), dev.UserCode)
	interval := time.Duration(max(dev.Interval, 1)) * time.Second
	deadline := time.Now().Add(time.Duration(max(dev.ExpiresIn, 60)) * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(interval):
		}
		var tok map[string]any
		st, err := getJSON(ctx, c, http.MethodPost, cfg.Issuer+"/protocol/openid-connect/token", url.Values{"client_id": {cfg.CLIClientID},
			"grant_type": {"urn:ietf:params:oauth:grant-type:device_code"}, "device_code": {dev.DeviceCode}}, &tok)
		if err != nil {
			return nil, err
		}
		if st == http.StatusOK {
			exp, _ := tok["expires_in"].(float64)
			access, _ := tok["access_token"].(string)
			refresh, _ := tok["refresh_token"].(string)
			return &Credentials{Manager: manager, Issuer: cfg.Issuer, Client: cfg.CLIClientID, Access: access, Refresh: refresh,
				Expiry: time.Now().Add(time.Duration(exp) * time.Second)}, nil
		}
		switch tok["error"] {
		case "authorization_pending":
		case "slow_down":
			interval += 5 * time.Second
		default:
			return nil, fmt.Errorf("device token: %v %v", tok["error"], tok["error_description"])
		}
	}
	return nil, errors.New("장치 코드 만료 — 다시 atlas login")
}

func SaveCredentials(cr *Credentials) error {
	p := CredentialsPath()
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	all := map[string]*Credentials{}
	if b, err := os.ReadFile(p); err == nil {
		_ = json.Unmarshal(b, &all)
	}
	all[cr.Manager] = cr
	b, _ := json.MarshalIndent(all, "", "  ")
	return os.WriteFile(p, b, 0o600)
}

// Token 은 저장된 토큰을 돌려준다(만료 30초 전이면 refresh).
func Token(ctx context.Context, c *http.Client, manager string) (string, error) {
	manager = strings.TrimRight(manager, "/")
	b, err := os.ReadFile(CredentialsPath())
	if err != nil {
		return "", errors.New("로그인 필요 — atlas login")
	}
	all := map[string]*Credentials{}
	_ = json.Unmarshal(b, &all)
	cr := all[manager]
	if cr == nil {
		return "", errors.New("이 Manager 로그인 없음 — atlas login --manager " + manager)
	}
	if time.Until(cr.Expiry) > 30*time.Second {
		return cr.Access, nil
	}
	var tok map[string]any
	st, err := getJSON(ctx, c, http.MethodPost, cr.Issuer+"/protocol/openid-connect/token",
		url.Values{"client_id": {cr.Client}, "grant_type": {"refresh_token"}, "refresh_token": {cr.Refresh}}, &tok)
	if err != nil || st != http.StatusOK {
		return "", errors.New("세션 만료 — atlas login")
	}
	cr.Access, _ = tok["access_token"].(string)
	if r, ok := tok["refresh_token"].(string); ok {
		cr.Refresh = r
	}
	exp, _ := tok["expires_in"].(float64)
	cr.Expiry = time.Now().Add(time.Duration(exp) * time.Second)
	return cr.Access, SaveCredentials(cr)
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}
