// atlas-manager — 컨트롤 플레인 (PRD §11 · §17.2). 설정은 env 주입(.env.local | .env.prod).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"atlas-manager/internal/api"
	"atlas-manager/internal/deploy"
	"atlas-manager/internal/events"
	"atlas-manager/internal/iam"
	"atlas-manager/internal/notify"
	"atlas-manager/internal/secrets"
	"atlas-manager/internal/store"
	"atlas-manager/internal/ui"
)

// version 은 빌드 시 -ldflags "-X main.version=…" 로 주입된다 (SemVer, FR-PLT-REL-01).
var version = "0.0.0-dev"

func main() {
	healthcheck := flag.Bool("healthcheck", false, "GET /health 후 종료코드로 결과 반환 (컨테이너 헬스체크용)")
	migrateOnly := flag.Bool("migrate", false, "마이그레이션만 적용하고 종료")
	flag.Parse()
	if *healthcheck {
		os.Exit(probe("http://127.0.0.1:8080/health"))
	}
	if err := run(*migrateOnly); err != nil {
		slog.Error("manager exited", "err", err)
		os.Exit(1)
	}
}

func run(migrateOnly bool) error {
	engine := env("DB_ENGINE", "sqlite")
	db, err := store.Open(engine, env("DATABASE_URL", "file:/app/data/app.db"))
	if err != nil {
		return err
	}
	if migrateOnly || env("ATLAS_AUTO_MIGRATE", "true") == "true" {
		if err := store.Migrate(db, engine); err != nil {
			return err
		}
		slog.Info("migrations applied", "engine", engine)
	}
	if migrateOnly {
		return nil
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	var pub events.Publisher = events.Nop{}
	var js *events.JetStream
	if url := os.Getenv("NATS_URL"); url != "" {
		js, err = events.Connect(ctx, url, "atlas-manager", "PLT", []string{"plt.>"})
		if err != nil {
			return err
		}
		defer js.Close()
		if err := js.EnsureStream(ctx, "AUDIT", []string{"audit.>"}); err != nil {
			return err
		}
		pub = js
	}

	interval, err := time.ParseDuration(env("ATLAS_PROBE_INTERVAL", "10s"))
	if err != nil {
		return err
	}
	prober := &deploy.Prober{DB: db, Events: pub, Client: &http.Client{Timeout: 3 * time.Second}, Interval: interval,
		FailThreshold: 3, StartupTimeout: 120 * time.Second, Now: func() time.Time { return time.Now().UTC() }}
	go prober.Run(ctx)
	as, err := iamSetup(ctx, db, engine)
	if err != nil {
		return err
	}
	as.DB, as.Events, as.Env, as.Engine, as.Version = db, pub, env("APP_ENV", "local"), engine, version
	as.Proxy, as.Services, as.UI = &http.Client{Timeout: 60 * time.Second}, api.ParseServices(os.Getenv("ATLAS_SVC_URLS")), ui.Handler()
	go func() { // 진행 중 롤아웃 자동 롤백 점검 (FR-PLT-DEP-05)
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				as.EvaluateRollouts(ctx)
			}
		}
	}()

	as.Notify = &notify.Engine{DB: db, HTTP: &http.Client{Timeout: 10 * time.Second}, SMTPAddr: os.Getenv("ATLAS_SMTP_ADDR"),
		From: env("ATLAS_SMTP_FROM", "atlas@localhost"), AllowHosts: notify.Hosts(os.Getenv("ATLAS_NOTIFY_ALLOW_HOSTS")), Now: time.Now}
	if u := os.Getenv("ATLAS_RPT_INGEST_URL"); u != "" { // X-RPT 수집기 (내부 1st-party — 허용 호스트에 자동 추가)
		if pu, err := url.Parse(u); err == nil {
			as.Notify.AllowHosts[pu.Hostname()] = true
			as.Notify.Forward = &notify.Channel{Type: "webhook", URL: u, Secret: os.Getenv("ATLAS_RPT_INGEST_SECRET")}
		}
	}
	go func() { // 다이제스트 발송·미확인 에스컬레이션 (X-NTF-04·05)
		t := time.NewTicker(5 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				as.Notify.FlushDigests(ctx)
				as.Notify.EscalateDue(ctx)
				as.EscalateApprovals(ctx) // 승인 SLA 초과 → plt.approval.escalated (X-HITL-05)
			}
		}
	}()
	if js != nil {
		as.Notify.Run(ctx, js)      // 이벤트 → 규칙 → webhook·email (X-NTF)
		go as.ConsumeUsage(ctx, js) // Gateway 사용량 → 조직 월 토큰 (X-WSP-08)
	}
	go func() { // 승인 만료 (X-HITL-01): 만료된 pending → expired + 요청 에이전트에 deny
		t := time.NewTicker(15 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				as.ExpireApprovals(ctx)
			}
		}
	}()
	srv := &http.Server{Addr: env("ATLAS_LISTEN", ":8080"), Handler: as.Router(), ReadHeaderTimeout: 10 * time.Second}
	slog.Info("console", "bundled", ui.Bundled(), "version", version)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	slog.Info("atlas-manager listening", "addr", srv.Addr, "engine", engine)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}

// iamSetup 은 인증·인가·감사 구성이다 (PLT-IAM · PLT-TEN).
func iamSetup(ctx context.Context, db *gorm.DB, engine string) (*api.Server, error) {
	s := &api.Server{AuthMode: env("ATLAS_AUTH_MODE", api.AuthOff), Audit: &iam.Audit{DB: db, Engine: engine}, KeyEnv: env("APP_ENV", "local")}
	signer, ephemeral, err := iam.NewSigner(os.Getenv("ATLAS_A2A_SIGNING_KEY"), "atlas-manager")
	if err != nil {
		return nil, err
	}
	if ephemeral {
		slog.Warn("A2A signing key not provided — ephemeral key (single instance only)")
	}
	s.Signer = signer
	if s.AuthMode == api.AuthOIDC {
		issuer := os.Getenv("ATLAS_OIDC_ISSUER")
		if issuer == "" {
			return nil, fmt.Errorf("ATLAS_AUTH_MODE=oidc 에는 ATLAS_OIDC_ISSUER 필요")
		}
		s.OIDC = iam.NewOIDC(ctx, issuer, env("ATLAS_OIDC_JWKS_URL", issuer+"/protocol/openid-connect/certs"), os.Getenv("ATLAS_OIDC_AUDIENCE"))
		s.OIDCPublic = gin.H{"issuer": issuer, "clientId": env("ATLAS_OIDC_CONSOLE_CLIENT", "atlas-console"), "cliClientId": env("ATLAS_OIDC_CLI_CLIENT", "atlas-cli")}
	}
	if u := os.Getenv("ATLAS_FGA_URL"); u != "" {
		s.FGA = &iam.FGA{URL: strings.TrimRight(u, "/"), Token: os.Getenv("ATLAS_FGA_TOKEN"), Client: &http.Client{Timeout: 5 * time.Second}}
		go func() { // OpenFGA 기동을 기다리며 스토어·모델·구조 튜플을 맞춘다
			if err := s.FGA.Bootstrap(ctx); err != nil {
				slog.Error("openfga bootstrap failed", "err", err)
				return
			}
			var orgs []store.Organization
			var envs []store.Environment
			db.Find(&orgs)
			db.Find(&envs)
			var ts []iam.Tuple
			for _, o := range orgs {
				ts = append(ts, iam.Structure(o.Slug, "", nil)...)
			}
			for _, e := range envs {
				ts = append(ts, iam.Structure(e.OrgID, e.Project, []string{e.Name})...)
			}
			if err := s.FGA.Write(ctx, ts, nil); err != nil {
				slog.Error("openfga structure sync failed", "err", err)
				return
			}
			slog.Info("openfga ready", "tuples", len(ts))
		}()
	}
	// 비밀 저장소 (FR-PLT-SEC-01): file:// (로컬) · bao:// (OpenBao KV v2)
	if uri := os.Getenv("ATLAS_SECRET_STORE"); uri != "" {
		st, err := secrets.Open(uri)
		if err != nil {
			return nil, fmt.Errorf("secret store: %w", err)
		}
		s.Secrets = st
		if bs, ok := st.(*secrets.BaoStore); ok {
			go bs.RenewLoop(ctx, 12*time.Hour) // 주기 토큰 갱신
		}
		if spec := os.Getenv("ATLAS_SECRET_IMPORT"); spec != "" { // env 주입 시절 비밀을 저장소로 1회 이관
			imported, err := api.ImportSecrets(ctx, st, spec, os.Getenv)
			if err != nil {
				slog.Error("secret import failed", "err", err)
			} else if len(imported) > 0 {
				slog.Info("secrets imported from env", "names", imported)
			}
		}
		slog.Info("secret store", "kind", st.Kind())
	}
	// 에이전트 자가 등록·MCP 도구용 서비스 키 (FR-PLT-IAM-06). 값은 env 로만 주입.
	if plain := os.Getenv("ATLAS_MANAGER_BOOTSTRAP_KEY"); plain != "" && !strings.Contains(plain, "CHANGE_ME") {
		prefix, err := api.BootstrapAPIKey(db, plain, "default", "read agents:write deployments:write a2a:invoke secrets:write")
		if err != nil {
			return nil, fmt.Errorf("bootstrap api key: %w", err)
		}
		slog.Info("bootstrap api key upserted", "prefix", prefix)
	}
	slog.Info("auth", "mode", s.AuthMode, "fga", s.FGA != nil)
	return s, nil
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
