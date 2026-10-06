// atlas-gateway — AI Gateway 데이터 플레인 (PRD §8 · GW-* 6종). 설정은 env 주입(.env.local | .env.prod).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"atlas-gateway/internal/aliases"
	"atlas-gateway/internal/audit"
	"atlas-gateway/internal/auth"
	"atlas-gateway/internal/config"
	"atlas-gateway/internal/cost"
	"atlas-gateway/internal/econ"
	"atlas-gateway/internal/events"
	"atlas-gateway/internal/guard"
	"atlas-gateway/internal/keyrot"
	"atlas-gateway/internal/provider"
	"atlas-gateway/internal/ratelimit"
	"atlas-gateway/internal/router"
	"atlas-gateway/internal/secrets"
	"atlas-gateway/internal/server"
	"atlas-gateway/internal/telemetry"
)

var version = "0.0.0-dev" // -ldflags "-X main.version=…"

func main() {
	healthcheck := flag.Bool("healthcheck", false, "GET /health 후 종료코드로 결과 반환 (컨테이너 헬스체크용)")
	verify := flag.Bool("verify-audit", false, "stdin 의 로그에서 감사 해시 체인을 검증")
	flag.Parse()
	if *healthcheck {
		os.Exit(probe("http://127.0.0.1:8080/health"))
	}
	if *verify {
		n, err := audit.Verify(os.Stdin)
		if err != nil {
			slog.Error("audit chain invalid", "records", n, "err", err)
			os.Exit(1)
		}
		slog.Info("audit chain ok", "records", n)
		return
	}
	if err := run(); err != nil {
		slog.Error("gateway exited", "err", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	shutdownTrace, err := telemetry.Init(ctx, "atlas-gateway", env("APP_ENV", "local"))
	if err != nil {
		return err
	}
	defer func() { _ = shutdownTrace(context.Background()) }()

	cfg, err := config.Load(os.Getenv("GATEWAY_CONFIG"))
	if err != nil {
		return err
	}
	prices, err := cost.NewTable(cfg.Prices)
	if err != nil {
		return err
	}
	opt, err := redis.ParseURL(env("REDIS_URL", "redis://redis:6379/0"))
	if err != nil {
		return err
	}
	rdb := redis.NewClient(opt)
	appEnv := env("APP_ENV", "local")
	keys := auth.NewStore(rdb, appEnv)

	var pub events.Publisher = events.Nop{}
	if url := os.Getenv("NATS_URL"); url != "" {
		js, err := events.Connect(ctx, url, "atlas-gateway", "GW", []string{"gw.>"})
		if err != nil {
			return err
		}
		defer js.Close()
		pub = js
	}

	mock := provider.Mock{Latency: time.Duration(envInt("MOCK_LATENCY_MS")) * time.Millisecond}
	providers := map[string]provider.Provider{"mock": mock}
	batchers := map[string]provider.BatchProvider{"mock": &provider.MockBatch{Mock: mock, Delay: time.Duration(cfg.Econ.Batch.MockDelaySec) * time.Second}}
	pools := map[string]*provider.KeyPool{}
	if base := os.Getenv("OPENAI_BASE_URL"); base != "" {
		pool := provider.NewKeyPool("openai", strings.Split(os.Getenv("OPENAI_API_KEYS"), ","))
		pool.Unhealthy = func(p string, i int) {
			_ = pub.Publish(context.Background(), "gw.key.unhealthy", map[string]any{"provider": p, "index": i})
		}
		oai := provider.OpenAI{BaseURL: strings.TrimRight(base, "/"), Pool: pool, Client: &http.Client{Timeout: 60 * time.Second}}
		providers["openai"], batchers["openai"] = oai, oai
		pools["openai"] = pool
	}

	// 부트스트랩 가상키 (예: 로컬 AL-PLAN 용). 값은 env 로만 주입.
	if plain := os.Getenv("ATLAS_GATEWAY_BOOTSTRAP_KEY"); plain != "" && !strings.Contains(plain, "CHANGE_ME") {
		if err := waitRedis(ctx, rdb); err != nil {
			return err
		}
		vk := auth.VirtualKey{Tenant: env("ATLAS_GATEWAY_BOOTSTRAP_TENANT", "default"), Project: "default", Models: []string{"*"},
			RPM: envInt("ATLAS_GATEWAY_BOOTSTRAP_RPM"), TPM: envInt("ATLAS_GATEWAY_BOOTSTRAP_TPM")}
		if err := keys.Put(ctx, plain, vk); err != nil {
			return err
		}
		got, _ := keys.Lookup(ctx, plain)
		slog.Info("bootstrap virtual key upserted", "prefix", got.Prefix)
	}

	var gc *guard.Client
	if url := os.Getenv("ATLAS_GUARD_URL"); url != "" {
		gc = guard.New(strings.TrimRight(url, "/"), time.Duration(max(100, envInt("ATLAS_GUARD_TIMEOUT_MS")))*time.Millisecond,
			env("ATLAS_GUARD_ON_ERROR", "fail_open") != "fail_closed")
	}
	// 순서 보장 발행 큐 — 요청 경로를 막지 않으면서 같은 정산의 임계 이벤트(50→80→100%)가 순서대로 나간다
	type outEvent struct {
		subject string
		data    any
	}
	queue := make(chan outEvent, 4096)
	go func() {
		for e := range queue {
			c, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			if err := pub.Publish(c, e.subject, e.data); err != nil {
				slog.Warn("event publish failed", "subject", e.subject, "err", err)
			}
			cancel()
		}
	}()
	publish := func(_ context.Context, subject string, data any) {
		select {
		case queue <- outEvent{subject, data}:
		default:
			slog.Warn("event queue full — dropped", "subject", subject)
		}
	}
	routes := router.New(cfg, providers)
	soft := max(1, cfg.Econ.SoftPct)
	s := &server.Server{Version: version, Guard: gc, Capture: env("ATLAS_OB_CAPTURE", "redacted"), Cfg: cfg, Redis: rdb, Keys: keys, Limiter: ratelimit.New(rdb), Routes: routes,
		Prices: prices, Audit: audit.New(os.Stdout), Events: pub, Pools: pools,
		AdminToken: os.Getenv("ATLAS_GATEWAY_ADMIN_TOKEN"), Env: appEnv, Now: time.Now,
		Budgets: &econ.Budgets{R: rdb, Now: time.Now, SoftPct: soft, Publish: publish},
		Tasks:   &econ.Tasks{R: rdb, Default: cfg.Econ.TaskBudgetTokens, SoftPct: soft, Publish: publish},
		Aliases: &aliases.Store{R: rdb, Cfg: cfg, Now: time.Now},
		Compute: &econ.Compute{R: rdb, Now: time.Now, Publish: publish}}
	go s.Aliases.Sync(ctx, 2*time.Second, routes.SetOverlay) // alias 오버레이 (BL-MODEL) — 인스턴스 간 공유
	discount, ok := new(big.Rat).SetString(cfg.Econ.BatchDiscount)
	if !ok {
		discount = new(big.Rat)
	}
	pollBase := time.Duration(max(1, envIntOr("ATLAS_BATCH_POLL_BASE_SEC", cfg.Econ.Batch.PollBaseSec))) * time.Second
	s.Batches = &econ.Batches{R: rdb, Cfg: cfg, Resolve: routes.Resolve, Providers: batchers, Prices: prices.Cost, Discount: discount,
		Now: time.Now, PollBase: pollBase, PollMax: max(pollBase, time.Duration(cfg.Econ.Batch.PollMaxSec)*time.Second),
		MaxRows: cfg.Econ.Batch.MaxRows, Publish: publish, OnCost: s.BatchCost}
	go s.Batches.Run(ctx, time.Second) // 폴링 스케줄러 (Redis 락으로 인스턴스 간 중복 방지)

	// 공급자 키 — 비밀 저장소가 정본, 무중단 회전 (FR-PLT-SEC-01·04)
	if uri := os.Getenv("ATLAS_SECRET_STORE"); uri != "" && len(pools) > 0 {
		st, err := secrets.Open(uri)
		if err != nil {
			return fmt.Errorf("secret store: %w", err)
		}
		s.KeyRot = &keyrot.Manager{Store: st, Pools: pools, Publish: publish, Validate: func(ctx context.Context, p, key string) error {
			o, ok := providers[p].(provider.OpenAI)
			if !ok {
				return nil
			}
			return o.Probe(ctx, key) // GET {base}/models 로 새 키 검증
		}}
		if err := s.KeyRot.Load(ctx); err != nil {
			return fmt.Errorf("provider keys: %w", err)
		}
		if bs, ok := st.(*secrets.BaoStore); ok {
			go bs.RenewLoop(ctx, 12*time.Hour)
		}
		go s.KeyRot.Run(ctx, 30*time.Second)
	}
	srv := &http.Server{Addr: ":8080", Handler: s.Router(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	slog.Info("atlas-gateway listening", "addr", srv.Addr, "aliases", len(cfg.Aliases), "providers", len(providers))
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}

func waitRedis(ctx context.Context, rdb *redis.Client) error {
	var err error
	for range 30 {
		if err = rdb.Ping(ctx).Err(); err == nil {
			return nil
		}
		time.Sleep(time.Second)
	}
	return err
}

func probe(url string) int {
	client := http.Client{Timeout: 2 * time.Second}
	res, err := client.Get(url)
	if err != nil || res.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string) int {
	n, _ := strconv.Atoi(os.Getenv(k))
	return n
}

func envIntOr(k string, def int) int {
	if n, err := strconv.Atoi(os.Getenv(k)); err == nil && n > 0 {
		return n
	}
	return def
}
