// Package guard 는 GR-IO 가드레일 서비스 클라이언트다 — Gateway 의 pre_call(input)·post_call(output) 훅 (FR-GW-POL-02).
package guard

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

const (
	Allow, Block, Transform = "allow", "block", "transform"
	Unavailable             = "unavailable"
)

type Client struct {
	URL      string
	HTTP     *http.Client
	FailOpen bool // 가드 장애 시 통과(true) 또는 차단(false)
}

type Verdict struct {
	ID         string   `json:"verdict_id"`
	Decision   string   `json:"decision"`
	Reasons    []string `json:"reasons"`
	Categories []string `json:"categories"`
	Contents   []string `json:"contents,omitempty"`
}

func New(url string, timeout time.Duration, failOpen bool) *Client {
	return &Client{URL: url, HTTP: &http.Client{Timeout: timeout}, FailOpen: failOpen}
}

// Check 는 boundary 에 contents 를 검사한다. 가드에 닿지 못하면 Decision=unavailable 과 오류를 함께 돌려준다.
func (c *Client) Check(ctx context.Context, tenant, boundary, session string, contents []string) (Verdict, error) {
	body, _ := json.Marshal(map[string]any{"tenant": tenant, "boundary": boundary, "contents": contents, "context": map[string]any{"session": session}})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, c.URL+"/v1/guard/check", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
	res, err := c.HTTP.Do(req)
	if err != nil {
		return Verdict{Decision: Unavailable}, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return Verdict{Decision: Unavailable}, fmt.Errorf("guard HTTP %d", res.StatusCode)
	}
	var v Verdict
	if err := json.NewDecoder(res.Body).Decode(&v); err != nil {
		return Verdict{Decision: Unavailable}, err
	}
	return v, nil
}
