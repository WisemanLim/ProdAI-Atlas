// Package aliases 는 런타임 alias 오버레이다 (FR-BL-MODEL-02·05·06): 코드·설정 재배포 없이 alias 가 가리키는 모델·가중치를 바꾸고,
// 모든 변경을 버전으로 남겨 롤백한다. 정본은 Redis(인스턴스 공유), 각 Gateway 는 주기적으로 읽어 라우터에 반영한다.
// alias·모델별 서빙 통계(요청·오류·비용·지연)는 카나리 판단 근거로 버전 단위로 모은다.
package aliases

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"atlas-gateway/internal/config"
)

const (
	keyOverlay = "atlas:gw:aliases"
	keyHist    = "atlas:gw:aliashist:"
	keyStats   = "atlas:gw:aliasstats:"
)

type Entry struct {
	Name        string              `json:"name"`
	Deployments []config.Deployment `json:"deployments"`
	Fallbacks   []string            `json:"fallbacks"`
	Tier        string              `json:"tier,omitempty"`
	MaxOut      int                 `json:"default_max_out,omitempty"`
	Version     int                 `json:"version"`
	Note        string              `json:"note,omitempty"`
	By          string              `json:"by,omitempty"`
	UpdatedAt   time.Time           `json:"updated_at"`
	Source      string              `json:"source"` // overlay | config
}

func (e Entry) Alias() config.Alias {
	return config.Alias{Deployments: e.Deployments, Fallbacks: e.Fallbacks, Tier: e.Tier, DefaultMaxOut: e.MaxOut, Version: e.Version}
}

type Store struct {
	R   *redis.Client
	Cfg *config.Config
	Now func() time.Time
}

var ErrInvalid = errors.New("invalid alias")

// Validate: 배포 1개 이상·가중치 합 > 0·모델은 카탈로그 또는 가격표에 있어야 하고, 폴백은 알려진 alias.
func (s *Store) Validate(e *Entry, known func(string) bool) error {
	if len(e.Deployments) == 0 {
		return fmt.Errorf("%w: deployments 비어 있음", ErrInvalid)
	}
	total := 0
	for _, d := range e.Deployments {
		if d.Weight < 0 || d.Provider == "" || d.Model == "" {
			return fmt.Errorf("%w: 배포 provider·model 필수, weight ≥ 0", ErrInvalid)
		}
		if _, ok := s.Cfg.Models[d.Model]; !ok {
			if _, priced := s.Cfg.Prices[d.Model]; !priced {
				return fmt.Errorf("%w: 카탈로그에 없는 모델 %s", ErrInvalid, d.Model)
			}
		}
		total += d.Weight
	}
	if total == 0 {
		return fmt.Errorf("%w: 가중치 합이 0", ErrInvalid)
	}
	for _, f := range e.Fallbacks {
		if f == e.Name || !known(f) {
			return fmt.Errorf("%w: 알 수 없는 fallback %s", ErrInvalid, f)
		}
	}
	return nil
}

func (s *Store) All(ctx context.Context) (map[string]Entry, error) {
	raw, err := s.R.HGetAll(ctx, keyOverlay).Result()
	if err != nil {
		return nil, err
	}
	out := map[string]Entry{}
	for k, v := range raw {
		var e Entry
		if json.Unmarshal([]byte(v), &e) == nil {
			out[k] = e
		}
	}
	return out, nil
}

// Current 는 유효 alias 다 (오버레이 없으면 설정 파일 = version 0).
func (s *Store) Current(ctx context.Context, name string) (Entry, bool) {
	if v, err := s.R.HGet(ctx, keyOverlay, name).Result(); err == nil {
		var e Entry
		if json.Unmarshal([]byte(v), &e) == nil {
			return e, true
		}
	}
	a, ok := s.Cfg.Aliases[name]
	if !ok {
		return Entry{}, false
	}
	fb := a.Fallbacks
	if fb == nil {
		fb = []string{}
	}
	return Entry{Name: name, Deployments: a.Deployments, Fallbacks: fb, Tier: a.Tier, MaxOut: a.DefaultMaxOut, Source: "config"}, true
}

// Put 은 새 버전을 쓴다. 직전 상태는 이력에 남는다.
func (s *Store) Put(ctx context.Context, e Entry) (Entry, error) {
	prev, had := s.Current(ctx, e.Name)
	if had {
		b, _ := json.Marshal(prev)
		s.R.LPush(ctx, keyHist+e.Name, b)
		s.R.LTrim(ctx, keyHist+e.Name, 0, 199)
	}
	if e.Tier == "" {
		e.Tier = prev.Tier
	}
	if e.MaxOut == 0 {
		e.MaxOut = prev.MaxOut
	}
	e.Version, e.UpdatedAt, e.Source = prev.Version+1, s.Now().UTC(), "overlay"
	if e.Fallbacks == nil {
		e.Fallbacks = []string{}
	}
	b, _ := json.Marshal(e)
	if err := s.R.HSet(ctx, keyOverlay, e.Name, b).Err(); err != nil {
		return Entry{}, err
	}
	return e, nil
}

func (s *Store) History(ctx context.Context, name string) []Entry {
	raw, _ := s.R.LRange(ctx, keyHist+name, 0, 199).Result()
	out := make([]Entry, 0, len(raw))
	for _, v := range raw {
		var e Entry
		if json.Unmarshal([]byte(v), &e) == nil {
			out = append(out, e)
		}
	}
	return out
}

// Rollback 은 이력의 version 내용을 새 버전으로 다시 쓴다 (원클릭 롤백, BL-MODEL-06).
func (s *Store) Rollback(ctx context.Context, name string, version int, by string) (Entry, error) {
	for _, h := range s.History(ctx, name) {
		if h.Version == version {
			h.Note, h.By = fmt.Sprintf("rollback to v%d", version), by
			return s.Put(ctx, h)
		}
	}
	return Entry{}, fmt.Errorf("%w: 이력에 v%d 없음", ErrInvalid, version)
}

// Delete 는 오버레이를 지워 설정 파일 alias 로 되돌린다(이력은 남김).
func (s *Store) Delete(ctx context.Context, name string) error {
	if prev, ok := s.Current(ctx, name); ok && prev.Source == "overlay" {
		b, _ := json.Marshal(prev)
		s.R.LPush(ctx, keyHist+name, b)
	}
	return s.R.HDel(ctx, keyOverlay, name).Err()
}

// Sync 는 주기적으로 오버레이를 읽어 apply(라우터)에 넘긴다.
func (s *Store) Sync(ctx context.Context, every time.Duration, apply func(map[string]config.Alias)) {
	load := func() {
		all, err := s.All(ctx)
		if err != nil {
			slog.Warn("alias overlay sync failed", "err", err)
			return
		}
		m := make(map[string]config.Alias, len(all))
		for k, e := range all {
			m[k] = e.Alias()
		}
		apply(m)
	}
	load()
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			load()
		}
	}
}

// ---- 서빙 통계 (alias 버전 × 모델) ----

func statsKey(alias string, version int, model string) string {
	return keyStats + alias + ":" + strconv.Itoa(version) + ":" + model
}

// Record 는 한 요청 결과를 더한다.
func (s *Store) Record(ctx context.Context, alias string, version int, model string, ok bool, costE8 int64, latency time.Duration) {
	k := statsKey(alias, version, model)
	p := s.R.TxPipeline()
	p.HIncrBy(ctx, k, "requests", 1)
	if !ok {
		p.HIncrBy(ctx, k, "errors", 1)
	}
	p.HIncrBy(ctx, k, "cost_e8", costE8)
	p.LPush(ctx, k+":lat", latency.Milliseconds())
	p.LTrim(ctx, k+":lat", 0, 999)
	p.Expire(ctx, k, 30*24*time.Hour)
	p.Expire(ctx, k+":lat", 30*24*time.Hour)
	_, _ = p.Exec(ctx)
}

type ModelStats struct {
	Model    string  `json:"model"`
	Requests int64   `json:"requests"`
	Errors   int64   `json:"errors"`
	ErrRate  float64 `json:"error_rate"`
	CostUSD  float64 `json:"cost_usd"`
	CostPer  float64 `json:"cost_per_request_usd"`
	P95MS    int64   `json:"p95_ms"`
}

func (s *Store) Stats(ctx context.Context, alias string, version int, models []string) []ModelStats {
	out := []ModelStats{}
	for _, m := range models {
		k := statsKey(alias, version, m)
		h, _ := s.R.HGetAll(ctx, k).Result()
		st := ModelStats{Model: m}
		st.Requests, _ = strconv.ParseInt(h["requests"], 10, 64)
		st.Errors, _ = strconv.ParseInt(h["errors"], 10, 64)
		ce, _ := strconv.ParseInt(h["cost_e8"], 10, 64)
		st.CostUSD = float64(ce) / 1e8
		if st.Requests > 0 {
			st.ErrRate = float64(st.Errors) / float64(st.Requests)
			st.CostPer = st.CostUSD / float64(st.Requests)
		}
		lat, _ := s.R.LRange(ctx, k+":lat", 0, 999).Result()
		ls := make([]int64, 0, len(lat))
		for _, v := range lat {
			n, _ := strconv.ParseInt(v, 10, 64)
			ls = append(ls, n)
		}
		if len(ls) > 0 {
			sort.Slice(ls, func(i, j int) bool { return ls[i] < ls[j] })
			st.P95MS = ls[int(0.95*float64(len(ls)-1))]
		}
		out = append(out, st)
	}
	return out
}
