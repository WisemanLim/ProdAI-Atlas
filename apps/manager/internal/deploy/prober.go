// Package deploy 는 배포 상태 수집기다 (FR-PLT-MGR-05): 드라이버 공통으로 HTTP /readyz 폴링 + Agent Card 수집·검증(FR-PLT-MGR-02).
// Docker 소켓을 마운트하지 않는다(보안) — compose 드라이버도 HTTP 폴링으로 상태를 본다.
package deploy

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"gorm.io/gorm"

	"atlas-manager/internal/events"
	"atlas-manager/internal/store"
)

// Prober 설정. FailThreshold 회 연속 실패하면 degraded → failed, StartupTimeout 안에 ready 가 안 되면 failed.
type Prober struct {
	DB             *gorm.DB
	Events         events.Publisher
	Client         *http.Client
	Interval       time.Duration
	FailThreshold  int
	StartupTimeout time.Duration
	Now            func() time.Time
}

var cardRequired = []string{"name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills"}

func (p *Prober) Run(ctx context.Context) {
	t := time.NewTicker(p.Interval)
	defer t.Stop()
	for {
		p.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Tick 은 중지되지 않은 모든 배포를 한 번 점검한다.
func (p *Prober) Tick(ctx context.Context) {
	var deps []store.Deployment
	if err := p.DB.WithContext(ctx).Where("status <> ?", store.DeployStopped).Find(&deps).Error; err != nil {
		slog.Warn("prober list failed", "err", err)
		return
	}
	for _, d := range deps {
		p.probe(ctx, d)
	}
}

func (p *Prober) probe(ctx context.Context, d store.Deployment) {
	var agent store.Agent
	if err := p.DB.WithContext(ctx).First(&agent, "id = ?", d.AgentID).Error; err != nil {
		return
	}
	now := p.Now()
	upd := map[string]any{"last_probe_at": now}
	next, msg := d.Status, ""

	if err := p.get(ctx, d.Endpoint+"/readyz", nil); err != nil {
		switch d.Status {
		case store.DeployPending, store.DeployStarting:
			next, msg = store.DeployStarting, "readiness 대기: "+err.Error()
			if now.Sub(d.CreatedAt) > p.StartupTimeout {
				next, msg = store.DeployFailed, fmt.Sprintf("startup timeout %s: %v", p.StartupTimeout, err)
			}
		case store.DeployReady, store.DeployDegraded:
			upd["fail_count"] = d.FailCount + 1
			next, msg = store.DeployDegraded, err.Error()
			if d.FailCount+1 >= p.FailThreshold {
				next = store.DeployFailed
			}
		}
	} else {
		var card map[string]any
		if err := p.get(ctx, d.Endpoint+"/.well-known/agent-card.json", &card); err != nil {
			next, msg = store.DeployFailed, "agent card 조회 실패: "+err.Error()
		} else if err := validateCard(card, agent.Name); err != nil {
			next, msg = store.DeployFailed, err.Error()
		} else {
			b, _ := json.Marshal(card)
			upd["card"], upd["fail_count"] = string(b), 0
			next = store.DeployReady
			p.drift(ctx, d, card, upd)
		}
	}
	if next != d.Status || msg != deref(d.Message) {
		upd["status"], upd["updated_at"] = next, now
		upd["message"] = nilIfEmpty(msg)
	}
	if err := p.DB.WithContext(ctx).Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(upd).Error; err != nil {
		slog.Warn("prober update failed", "id", d.ID, "err", err)
		return
	}
	if next != d.Status {
		subject := "plt.deploy.progress"
		if next == store.DeployReady {
			subject = "plt.deploy.succeeded"
		}
		ectx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		_ = p.Events.Publish(ectx, subject, map[string]any{"id": d.ID, "agentId": d.AgentID, "env": d.Env, "from": d.Status, "to": next, "message": msg})
		slog.Info("deployment status", "id", d.ID, "agent", agent.Name, "from", d.Status, "to", next)
	}
}

// drift 는 실제 Agent Card(version·skills)와 배포 리비전 매니페스트(spec.version·agentCard.skills)를 비교한다 (FR-PLT-DEP-09).
func (p *Prober) drift(ctx context.Context, d store.Deployment, card map[string]any, upd map[string]any) {
	var rev store.Revision
	if p.DB.WithContext(ctx).First(&rev, "agent_id = ? AND rev = ?", d.AgentID, d.Rev).Error != nil {
		return
	}
	var m struct {
		Spec struct {
			Version   string `json:"version"`
			AgentCard struct {
				Skills []string `json:"skills"`
			} `json:"agentCard"`
		} `json:"spec"`
	}
	_ = json.Unmarshal([]byte(rev.Manifest), &m)
	var diffs []string
	if v, _ := card["version"].(string); m.Spec.Version != "" && v != m.Spec.Version {
		diffs = append(diffs, fmt.Sprintf("version %s ≠ 선언 %s", v, m.Spec.Version))
	}
	if len(m.Spec.AgentCard.Skills) > 0 {
		have := map[string]bool{}
		if skills, ok := card["skills"].([]any); ok {
			for _, sk := range skills {
				if mm, ok := sk.(map[string]any); ok {
					have[fmt.Sprint(mm["id"])] = true
				}
			}
		}
		for _, want := range m.Spec.AgentCard.Skills {
			if !have[want] {
				diffs = append(diffs, "skill "+want+" 없음")
			}
		}
	}
	cur := deref(d.Drift)
	nxt := strings.Join(diffs, "; ")
	if nxt == cur {
		return
	}
	upd["drift"] = nilIfEmpty(nxt)
	if nxt != "" {
		ectx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		_ = p.Events.Publish(ectx, "plt.deploy.drift", map[string]any{"org": d.OrgID, "id": d.ID, "agentId": d.AgentID, "env": d.Env, "rev": d.Rev, "drift": diffs})
	}
}

func (p *Prober) get(ctx context.Context, url string, out any) error {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	res, err := p.Client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", res.StatusCode)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(out)
}

// validateCard 는 A2A v1.0 필수 필드·이름 일치·skill 존재를 본다. JWS 서명 검증(FR-A2A-CARD-02)은 M1.
func validateCard(card map[string]any, agentName string) error {
	var missing []string
	for _, k := range cardRequired {
		if _, ok := card[k]; !ok {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("agent card 필수 필드 누락: %s", strings.Join(missing, ","))
	}
	if card["name"] != agentName {
		return fmt.Errorf("agent card name %v ≠ 등록 이름 %s", card["name"], agentName)
	}
	if skills, _ := card["skills"].([]any); len(skills) == 0 {
		return fmt.Errorf("agent card skills 비어 있음")
	}
	return nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
