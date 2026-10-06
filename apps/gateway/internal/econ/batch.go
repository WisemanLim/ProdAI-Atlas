package econ

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"sort"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"

	"atlas-gateway/internal/config"
	"atlas-gateway/internal/provider"
)

// 배치 (FR-EC-BATCH-01·02): POST /v1/batches(OpenAI 형식 JSONL) → alias 의 공급자 Batch API 로 제출 →
// 폴링 스케줄러(지수 간격 base→max) → 결과 수집(행별 custom_id 보존) → 실패 행만 재제출. 완료 시 gw.batch.completed.
// 결과 저장은 Redis(7일) — S3 호환 오브젝트 저장소는 M2 후속.

type Batch struct {
	ID              string            `json:"id"`
	Object          string            `json:"object"`
	Tenant          string            `json:"tenant"`
	Project         string            `json:"project,omitempty"`
	KeyPrefix       string            `json:"key_prefix"`
	Alias           string            `json:"alias"`
	Provider        string            `json:"provider"`
	Model           string            `json:"model"`
	ProviderBatchID string            `json:"provider_batch_id"`
	Status          string            `json:"status"`
	Round           int               `json:"round"`
	Total           int               `json:"total"`
	Completed       int               `json:"completed"`
	Failed          int               `json:"failed"`
	CostUSD         string            `json:"cost_usd"`
	SavingsUSD      string            `json:"savings_usd"`
	Polls           int               `json:"polls"`
	CreatedAt       time.Time         `json:"created_at"`
	UpdatedAt       time.Time         `json:"updated_at"`
	ExpiresAt       time.Time         `json:"expires_at"`
	NextPollAt      time.Time         `json:"next_poll_at"`
	EndedAt         *time.Time        `json:"ended_at,omitempty"`
	Metadata        map[string]string `json:"metadata,omitempty"`
	Error           string            `json:"error,omitempty"`
}

type Owner struct{ Tenant, Project, KeyPrefix string }

type Batches struct {
	R         *redis.Client
	Cfg       *config.Config
	Resolve   func(alias string) (provider.Provider, config.Deployment, bool)
	Providers map[string]provider.BatchProvider
	Prices    func(model string, input, output int, at time.Time) (*big.Rat, bool)
	Discount  *big.Rat
	Now       func() time.Time
	PollBase  time.Duration
	PollMax   time.Duration
	MaxRows   int
	Publish   func(ctx context.Context, subject string, data any)
	OnCost    func(ctx context.Context, b Batch, usd *big.Rat, prompt, completion int) // 사용량·예산 정산
}

var (
	ErrBatchInput    = errors.New("invalid batch input")
	ErrBatchNotFound = errors.New("batch not found")
	ErrBatchState    = errors.New("batch state")
)

const ttl = 7 * 24 * time.Hour

func bKey(id string) string { return "gw:batch:" + id }

type line struct {
	CustomID string `json:"custom_id"`
	Method   string `json:"method"`
	URL      string `json:"url"`
	Body     struct {
		Model       string             `json:"model"`
		Messages    []provider.Message `json:"messages"`
		MaxTokens   *int               `json:"max_tokens"`
		Temperature *float64           `json:"temperature"`
	} `json:"body"`
}

// Create 는 JSONL 을 검증해 제출한다. 모든 행은 같은 alias 여야 한다(공급사 배치는 모델 하나).
func (s *Batches) Create(ctx context.Context, o Owner, jsonl []byte, allows func(string) bool, meta map[string]string) (*Batch, error) {
	var rows []provider.BatchRow
	raw := map[string][]byte{}
	alias := ""
	sc := bufio.NewScanner(bytes.NewReader(jsonl))
	sc.Buffer(make([]byte, 1<<20), 8<<20)
	n := 0
	for sc.Scan() {
		n++
		b := bytes.TrimSpace(sc.Bytes())
		if len(b) == 0 {
			continue
		}
		var l line
		if err := json.Unmarshal(b, &l); err != nil {
			return nil, fmt.Errorf("%w: %d행 JSON 오류", ErrBatchInput, n)
		}
		switch {
		case l.CustomID == "":
			return nil, fmt.Errorf("%w: %d행 custom_id 필수", ErrBatchInput, n)
		case raw[l.CustomID] != nil:
			return nil, fmt.Errorf("%w: custom_id 중복 %s", ErrBatchInput, l.CustomID)
		case l.Method != "POST" || l.URL != "/v1/chat/completions":
			return nil, fmt.Errorf("%w: %d행 method=POST url=/v1/chat/completions 만 지원", ErrBatchInput, n)
		case len(l.Body.Messages) == 0:
			return nil, fmt.Errorf("%w: %d행 messages 필수", ErrBatchInput, n)
		}
		if alias == "" {
			alias = l.Body.Model
		} else if l.Body.Model != alias {
			return nil, fmt.Errorf("%w: 한 배치는 alias 하나 (%s ≠ %s)", ErrBatchInput, l.Body.Model, alias)
		}
		raw[l.CustomID] = append([]byte(nil), b...)
		rows = append(rows, provider.BatchRow{CustomID: l.CustomID, Req: s.toReq(l)})
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("%w: 빈 입력", ErrBatchInput)
	}
	if s.MaxRows > 0 && len(rows) > s.MaxRows {
		return nil, fmt.Errorf("%w: 최대 %d행", ErrBatchInput, s.MaxRows)
	}
	if _, ok := s.Cfg.Aliases[alias]; !ok {
		return nil, fmt.Errorf("%w: 알 수 없는 alias %s", ErrBatchInput, alias)
	}
	if !allows(alias) {
		return nil, fmt.Errorf("%w: 이 키는 %s 를 호출할 수 없음", ErrBatchInput, alias)
	}
	_, d, ok := s.Resolve(alias)
	bp := s.Providers[d.Provider]
	if !ok || bp == nil {
		return nil, fmt.Errorf("%w: 공급자 %s 는 Batch API 미지원", ErrBatchInput, d.Provider)
	}
	for i := range rows {
		rows[i].Req.Model = d.Model
	}
	now := s.Now()
	b := &Batch{ID: "batch_" + uuid.NewString(), Object: "batch", Tenant: o.Tenant, Project: o.Project, KeyPrefix: o.KeyPrefix,
		Alias: alias, Provider: d.Provider, Model: d.Model, Status: provider.BatchValidating, Total: len(rows),
		CreatedAt: now, UpdatedAt: now, ExpiresAt: now.Add(24 * time.Hour), Metadata: meta, CostUSD: "0.00000000", SavingsUSD: "0.00000000"}
	pid, err := bp.SubmitBatch(ctx, rows)
	if err != nil {
		return nil, fmt.Errorf("공급자 제출 실패: %w", err)
	}
	b.ProviderBatchID, b.Status, b.NextPollAt = pid, provider.BatchInProgress, now.Add(s.PollBase)
	p := s.R.TxPipeline()
	for id, v := range raw {
		p.HSet(ctx, bKey(b.ID)+":in", id, v)
	}
	p.Expire(ctx, bKey(b.ID)+":in", ttl)
	p.ZAdd(ctx, "gw:batches:t:"+o.Tenant, redis.Z{Score: float64(now.Unix()), Member: b.ID})
	p.SAdd(ctx, "gw:batches:active", b.ID)
	if _, err := p.Exec(ctx); err != nil {
		return nil, err
	}
	return b, s.save(ctx, b)
}

func (s *Batches) toReq(l line) provider.Request {
	mt := s.Cfg.Aliases[l.Body.Model].DefaultMaxOut
	if mt == 0 {
		mt = s.Cfg.Policy.DefaultMaxTokens
	}
	if l.Body.MaxTokens != nil {
		mt = *l.Body.MaxTokens
	}
	if c := s.Cfg.Policy.MaxTokensCap; c > 0 && mt > c {
		mt = c
	}
	return provider.Request{Messages: l.Body.Messages, MaxTokens: mt, Temperature: l.Body.Temperature}
}

func (s *Batches) save(ctx context.Context, b *Batch) error {
	raw, _ := json.Marshal(b)
	return s.R.Set(ctx, bKey(b.ID), raw, ttl).Err()
}

// Get 은 같은 tenant 의 배치만 돌려준다.
func (s *Batches) Get(ctx context.Context, tenant, id string) (*Batch, error) {
	raw, err := s.R.Get(ctx, bKey(id)).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, ErrBatchNotFound
	}
	if err != nil {
		return nil, err
	}
	var b Batch
	if json.Unmarshal(raw, &b) != nil || (tenant != "" && b.Tenant != tenant) {
		return nil, ErrBatchNotFound
	}
	return &b, nil
}

func (s *Batches) List(ctx context.Context, tenant string, limit int64) ([]Batch, error) {
	ids, err := s.R.ZRevRange(ctx, "gw:batches:t:"+tenant, 0, limit-1).Result()
	if err != nil {
		return nil, err
	}
	out := []Batch{}
	for _, id := range ids {
		if b, err := s.Get(ctx, tenant, id); err == nil {
			out = append(out, *b)
		}
	}
	return out, nil
}

// Tick 은 폴링 시각이 된 활성 배치를 하나씩 처리한다(인스턴스 간 중복은 Redis 락으로 막는다).
func (s *Batches) Tick(ctx context.Context) {
	ids, err := s.R.SMembers(ctx, "gw:batches:active").Result()
	if err != nil {
		return
	}
	sort.Strings(ids)
	for _, id := range ids {
		b, err := s.Get(ctx, "", id)
		if err != nil {
			s.R.SRem(ctx, "gw:batches:active", id)
			continue
		}
		if s.Now().Before(b.NextPollAt) {
			continue
		}
		if ok, _ := s.R.SetNX(ctx, bKey(id)+":lock", 1, 60*time.Second).Result(); !ok {
			continue
		}
		s.poll(ctx, b)
		s.R.Del(ctx, bKey(id)+":lock")
	}
}

func (s *Batches) Run(ctx context.Context, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.Tick(ctx)
		}
	}
}

func (s *Batches) poll(ctx context.Context, b *Batch) {
	now := s.Now()
	b.Polls++
	b.UpdatedAt = now
	bp := s.Providers[b.Provider]
	st, results, err := bp.PollBatch(ctx, b.ProviderBatchID)
	if err != nil {
		slog.Warn("batch poll failed", "batch", b.ID, "err", err)
		st = provider.BatchInProgress
	}
	if st != provider.BatchEnded && now.After(b.ExpiresAt) {
		st = provider.BatchExpired
	}
	switch st {
	case provider.BatchEnded:
		s.collect(ctx, b, results)
		return
	case provider.BatchFailed, provider.BatchExpired:
		b.Status, b.EndedAt = st, &now
		s.R.SRem(ctx, "gw:batches:active", b.ID)
		_ = s.save(ctx, b)
		s.publish(ctx, b)
		return
	}
	// 지수 간격: base·2^(polls-1), 상한 max
	d := s.PollBase << min(b.Polls-1, 16)
	if d > s.PollMax || d <= 0 {
		d = s.PollMax
	}
	b.Status, b.NextPollAt = st, now.Add(d)
	_ = s.save(ctx, b)
}

type outLine struct {
	ID       string `json:"id"`
	CustomID string `json:"custom_id"`
	Response *struct {
		StatusCode int            `json:"status_code"`
		Body       map[string]any `json:"body"`
	} `json:"response"`
	Error *struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func (s *Batches) collect(ctx context.Context, b *Batch, results []provider.BatchResult) {
	now := s.Now()
	total, saved := new(big.Rat), new(big.Rat)
	if prev, ok := new(big.Rat).SetString(b.CostUSD); ok {
		total = prev
	}
	if prev, ok := new(big.Rat).SetString(b.SavingsUSD); ok {
		saved = prev
	}
	inc, pTok, cTok := new(big.Rat), 0, 0 // 이번 회차 증분 (재제출 회차는 실패 행만)
	p := s.R.TxPipeline()
	for _, r := range results {
		ol := outLine{ID: "batch_req_" + uuid.NewString(), CustomID: r.CustomID}
		if r.Status == 200 && r.Response != nil {
			u := r.Response.Usage
			full, _ := s.Prices(b.Model, u.Prompt, u.Completion, b.CreatedAt)
			disc := new(big.Rat).Mul(full, new(big.Rat).Sub(big.NewRat(1, 1), s.Discount))
			total.Add(total, disc)
			inc.Add(inc, disc)
			saved.Add(saved, new(big.Rat).Sub(full, disc))
			pTok, cTok = pTok+u.Prompt, cTok+u.Completion
			msg := map[string]any{"role": "assistant", "content": r.Response.Content}
			if len(r.Response.ToolCalls) > 0 {
				msg["tool_calls"] = r.Response.ToolCalls
			}
			ol.Response = &struct {
				StatusCode int            `json:"status_code"`
				Body       map[string]any `json:"body"`
			}{200, map[string]any{"object": "chat.completion", "model": b.Alias,
				"choices": []any{map[string]any{"index": 0, "message": msg, "finish_reason": r.Response.FinishReason}},
				"usage":   map[string]int{"prompt_tokens": u.Prompt, "completion_tokens": u.Completion, "total_tokens": u.Prompt + u.Completion}}}
			p.HSet(ctx, bKey(b.ID)+":ok", r.CustomID, 1)
			p.HDel(ctx, bKey(b.ID)+":fail", r.CustomID)
		} else {
			ol.Error = &struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			}{fmt.Sprintf("status_%d", r.Status), r.Error}
			p.HSet(ctx, bKey(b.ID)+":fail", r.CustomID, 1)
		}
		raw, _ := json.Marshal(ol)
		p.HSet(ctx, bKey(b.ID)+":out", r.CustomID, raw)
	}
	for _, sfx := range []string{":out", ":ok", ":fail"} {
		p.Expire(ctx, bKey(b.ID)+sfx, ttl)
	}
	_, _ = p.Exec(ctx)
	okN, _ := s.R.HLen(ctx, bKey(b.ID)+":ok").Result()
	failN, _ := s.R.HLen(ctx, bKey(b.ID)+":fail").Result()
	b.Completed, b.Failed = int(okN), int(failN)
	b.CostUSD, b.SavingsUSD = total.FloatString(8), saved.FloatString(8)
	b.Status, b.EndedAt, b.UpdatedAt = provider.BatchEnded, &now, now
	s.R.SRem(ctx, "gw:batches:active", b.ID)
	_ = s.save(ctx, b)
	s.publish(ctx, b)
	if s.OnCost != nil && pTok+cTok > 0 {
		s.OnCost(ctx, *b, inc, pTok, cTok)
	}
}

func (s *Batches) publish(ctx context.Context, b *Batch) {
	if s.Publish != nil {
		s.Publish(ctx, "gw.batch.completed", map[string]any{"batch_id": b.ID, "tenant": b.Tenant, "alias": b.Alias, "status": b.Status,
			"total": b.Total, "completed": b.Completed, "failed": b.Failed, "cost_usd": b.CostUSD, "savings_usd": b.SavingsUSD, "round": b.Round})
	}
}

// Output 은 OpenAI 배치 출력 형식 JSONL 이다 (입력 순서와 무관, custom_id 로 대응).
func (s *Batches) Output(ctx context.Context, tenant, id string) ([]byte, error) {
	b, err := s.Get(ctx, tenant, id)
	if err != nil {
		return nil, err
	}
	if b.Status != provider.BatchEnded {
		return nil, fmt.Errorf("%w: 아직 %s", ErrBatchState, b.Status)
	}
	m, err := s.R.HGetAll(ctx, bKey(id)+":out").Result()
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	for _, k := range sortedKeys(m) {
		buf.WriteString(m[k])
		buf.WriteByte('\n')
	}
	return buf.Bytes(), nil
}

// Retry 는 실패 행만 다시 제출한다 (FR-EC-BATCH-02).
func (s *Batches) Retry(ctx context.Context, tenant, id string) (*Batch, error) {
	b, err := s.Get(ctx, tenant, id)
	if err != nil {
		return nil, err
	}
	if b.Status != provider.BatchEnded || b.Failed == 0 {
		return nil, fmt.Errorf("%w: 실패 행이 있는 ended 배치만 재제출 (status=%s failed=%d)", ErrBatchState, b.Status, b.Failed)
	}
	failed, err := s.R.HKeys(ctx, bKey(id)+":fail").Result()
	if err != nil {
		return nil, err
	}
	var rows []provider.BatchRow
	for _, cid := range failed {
		raw, err := s.R.HGet(ctx, bKey(id)+":in", cid).Bytes()
		if err != nil {
			continue
		}
		var l line
		if json.Unmarshal(raw, &l) == nil {
			r := s.toReq(l)
			r.Model = b.Model
			rows = append(rows, provider.BatchRow{CustomID: cid, Req: r})
		}
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("%w: 재제출할 입력 행 없음", ErrBatchState)
	}
	pid, err := s.Providers[b.Provider].SubmitBatch(ctx, rows)
	if err != nil {
		return nil, err
	}
	now := s.Now()
	b.ProviderBatchID, b.Status, b.Round, b.Polls = pid, provider.BatchInProgress, b.Round+1, 0
	b.NextPollAt, b.UpdatedAt, b.EndedAt, b.ExpiresAt = now.Add(s.PollBase), now, nil, now.Add(24*time.Hour)
	s.R.SAdd(ctx, "gw:batches:active", b.ID)
	return b, s.save(ctx, b)
}
