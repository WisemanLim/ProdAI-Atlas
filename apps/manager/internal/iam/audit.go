package iam

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"gorm.io/gorm"

	"atlas-manager/internal/store"
)

// Audit 은 조직별 해시 체인 감사 로그다 (FR-PLT-IAM-08): append 전용(UPDATE·DELETE 는 DB 트리거가 거부),
// hash = SHA-256(prev_hash ‖ 필드), 일 단위 머클 루트. 요청 트랜잭션과 분리된 기본 연결로 써서 실패한 요청도 남는다.
type Audit struct {
	DB     *gorm.DB
	Engine string
	mu     sync.Mutex
}

type Entry struct {
	Org, ActorType, ActorID, ActorIP, Action, Resource, Result string
	Before, After                                              any // 해시만 기록 (원문은 저장하지 않음)
	Detail                                                     map[string]any
}

func digest(v any) string {
	if v == nil {
		return ""
	}
	b, _ := json.Marshal(v)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func chainHash(e store.AuditEvent) string {
	h := sha256.New()
	for _, f := range []string{e.PrevHash, fmt.Sprint(e.Seq), e.TS.UTC().Format(time.RFC3339Nano), e.OrgID, e.ActorType, e.ActorID, e.ActorIP,
		e.Action, e.Resource, e.BeforeHash, e.AfterHash, e.Result, e.Detail} {
		h.Write([]byte(f))
		h.Write([]byte{0x1f})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func (a *Audit) Append(ctx context.Context, en Entry) (store.AuditEvent, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if en.Org == "" {
		en.Org = "platform"
	}
	if en.Result == "" {
		en.Result = "success"
	}
	detail, _ := json.Marshal(en.Detail)
	if en.Detail == nil {
		detail = []byte("{}")
	}
	var ev store.AuditEvent
	err := a.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if a.Engine == "postgres" { // 인스턴스 간 직렬화 (조직 체인 단위)
			if err := tx.Exec("SELECT pg_advisory_xact_lock(hashtext(?))", "audit:"+en.Org).Error; err != nil {
				return err
			}
		}
		var last store.AuditEvent
		prev, seq := "", int64(1)
		if err := tx.Where("org_id = ?", en.Org).Order("seq DESC").First(&last).Error; err == nil {
			prev, seq = last.Hash, last.Seq+1
		} else if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		ev = store.AuditEvent{ID: ULID(), Seq: seq, TS: time.Now().UTC().Truncate(time.Microsecond), OrgID: en.Org, ActorType: en.ActorType,
			ActorID: en.ActorID, ActorIP: en.ActorIP, Action: en.Action, Resource: en.Resource, BeforeHash: digest(en.Before),
			AfterHash: digest(en.After), Result: en.Result, Detail: string(detail), PrevHash: prev}
		ev.Hash = chainHash(ev)
		return tx.Create(&ev).Error
	})
	return ev, err
}

// Verify 는 조직 체인을 처음부터 다시 계산한다. 끊긴 곳이 있으면 그 seq 를 돌려준다.
func (a *Audit) Verify(ctx context.Context, org string) (count int64, brokenAt int64, err error) {
	var evs []store.AuditEvent
	if err := a.DB.WithContext(ctx).Where("org_id = ?", org).Order("seq").Find(&evs).Error; err != nil {
		return 0, 0, err
	}
	prev := ""
	for i, e := range evs {
		if e.Seq != int64(i+1) || e.PrevHash != prev || chainHash(e) != e.Hash {
			return int64(len(evs)), e.Seq, nil
		}
		prev = e.Hash
	}
	return int64(len(evs)), 0, nil
}

// Root 는 하루(UTC) 이벤트 해시들의 머클 루트다 — 외부 WORM·Rekor 앵커 대상.
func (a *Audit) Root(ctx context.Context, org string, day time.Time) (string, int, error) {
	start := time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, time.UTC)
	var evs []store.AuditEvent
	if err := a.DB.WithContext(ctx).Where("org_id = ? AND ts >= ? AND ts < ?", org, start, start.Add(24*time.Hour)).Order("seq").Find(&evs).Error; err != nil {
		return "", 0, err
	}
	if len(evs) == 0 {
		return "", 0, nil
	}
	level := make([][]byte, len(evs))
	for i, e := range evs {
		level[i], _ = hex.DecodeString(e.Hash)
	}
	for len(level) > 1 {
		var next [][]byte
		for i := 0; i < len(level); i += 2 {
			r := level[i]
			if i+1 < len(level) {
				r = level[i+1]
			}
			h := sha256.Sum256(append(append([]byte{}, level[i]...), r...))
			next = append(next, h[:])
		}
		level = next
	}
	return hex.EncodeToString(level[0]), len(evs), nil
}
