package store

import (
	"time"

	"gorm.io/gorm"
)

// Agent 는 카탈로그에 등록된 에이전트 정의다(AgentDefinition, PRD §17.1).
type Agent struct {
	ID         string         `gorm:"primaryKey" json:"id"`
	OrgID      string         `json:"org"`
	Project    string         `json:"project"`
	Name       string         `json:"name"`
	UnitID     string         `json:"unitId"`
	Layer      string         `json:"layer"`
	Concept    string         `json:"concept"`
	CurrentRev int            `json:"currentRev"`
	CreatedAt  time.Time      `json:"createdAt"`
	UpdatedAt  time.Time      `json:"updatedAt"`
	DeletedAt  gorm.DeletedAt `json:"-"`
}

// Revision 은 불변 리비전이다(FR-PLT-MGR-03). 같은 spec 해시면 새로 만들지 않는다.
type Revision struct {
	AgentID   string    `gorm:"primaryKey" json:"agentId"`
	Rev       int       `gorm:"primaryKey" json:"rev"`
	Version   string    `json:"version"`
	SpecHash  string    `json:"specHash"`
	Manifest  string    `json:"-"`
	OrgID     string    `json:"-"`
	CreatedAt time.Time `json:"createdAt"`
}

func (Revision) TableName() string { return "agent_revisions" }

// 배포 상태 (PRD FR-PLT-MGR-10: pending→starting→ready→degraded→failed, 중지 시 stopped).
const (
	DeployPending  = "pending"
	DeployStarting = "starting"
	DeployReady    = "ready"
	DeployDegraded = "degraded"
	DeployFailed   = "failed"
	DeployStopped  = "stopped"
)

// Deployment 는 에이전트 리비전을 환경에 배치한 인스턴스다(AgentInstance + Deployment, PRD §17.1 단순화).
type Deployment struct {
	ID          string     `gorm:"primaryKey" json:"id"`
	OrgID       string     `json:"org"`
	AgentID     string     `json:"agentId"`
	Rev         int        `json:"rev"`
	Env         string     `json:"env"`
	Driver      string     `json:"driver"`
	Endpoint    string     `json:"endpoint"`
	Status      string     `json:"status"`
	Message     *string    `json:"message,omitempty"`
	Card        *string    `json:"-"`
	FailCount   int        `json:"failCount"`
	LastProbeAt *time.Time `json:"lastProbeAt,omitempty"`
	CreatedAt   time.Time  `json:"createdAt"`
	UpdatedAt   time.Time  `json:"updatedAt"`
	// 00008: 슬롯(primary·canary·standby)·가중치(%)·드리프트·선언 복제 수·롤아웃·호출/오류 수 (FR-PLT-DEP-05~09 · PLT-MGR-07)
	Slot      string  `json:"slot"` // gorm default 태그 없음 — 0% 가중치(블루그린 green)가 DB 기본값 100 으로 바뀌지 않게
	Weight    int     `json:"weight"`
	Drift     *string `json:"drift,omitempty"`
	Replicas  int     `json:"replicas"`
	RolloutID *string `json:"rolloutId,omitempty"`
	Calls     int     `json:"calls"`
	Errors    int     `json:"errors"`
}

// BeforeCreate 는 슬롯을 비운 생성(기존 호출부·시험)을 primary 로 채운다. canary 의 0% 가중치는 그대로 둔다.
func (d *Deployment) BeforeCreate(*gorm.DB) error {
	if d.Slot == "" {
		d.Slot = SlotPrimary
	}
	if d.Slot == SlotPrimary && d.Weight == 0 {
		d.Weight = 100
	}
	if d.Replicas == 0 {
		d.Replicas = 1
	}
	return nil
}

// 배포 슬롯.
const (
	SlotPrimary = "primary"
	SlotCanary  = "canary"
	SlotStandby = "standby"
)

// Rollout 은 점진 배포다 (FR-PLT-DEP-05·06): canary = 가중치 단계, bluegreen = 0 → 100 전환. 상태 running·promoted·rolled_back.
type Rollout struct {
	ID               string    `gorm:"primaryKey" json:"id"`
	OrgID            string    `json:"org"`
	AgentID          string    `json:"agentId"`
	Env              string    `json:"env"`
	Strategy         string    `json:"strategy"`
	Rev              int       `json:"rev"`
	FromDeploymentID *string   `json:"fromDeploymentId,omitempty"`
	ToDeploymentID   string    `json:"toDeploymentId"`
	Steps            string    `json:"-"`
	StepIndex        int       `json:"stepIndex"`
	Status           string    `json:"status"`
	Gate             string    `json:"-"`
	Reason           *string   `json:"reason,omitempty"`
	History          string    `json:"-"`
	CreatedBy        string    `json:"createdBy"`
	CreatedAt        time.Time `json:"createdAt"`
	UpdatedAt        time.Time `json:"updatedAt"`
}

// DeployPolicy 는 환경별 배포 전 검사 정책이다 (FR-PLT-DEP-10).
type DeployPolicy struct {
	OrgID     string    `gorm:"primaryKey" json:"org"`
	Env       string    `gorm:"primaryKey" json:"env"`
	Policy    string    `json:"-"`
	UpdatedAt time.Time `json:"updatedAt"`
}

// TopologyEdge 는 관찰된 A2A 호출 간선이다 (FR-PLT-MGR-09): caller(주체) → callee(에이전트).
type TopologyEdge struct {
	OrgID  string    `gorm:"primaryKey" json:"org"`
	Env    string    `gorm:"primaryKey" json:"env"`
	Caller string    `gorm:"primaryKey" json:"caller"`
	Callee string    `gorm:"primaryKey" json:"callee"`
	Calls  int       `json:"calls"`
	Errors int       `json:"errors"`
	LastAt time.Time `json:"lastAt"`
}

// ---- PLT-TEN · PLT-IAM (00004) ----

// Organization 은 기업(테넌트)이다 (FR-PLT-TEN-01).
type Organization struct {
	ID          string     `gorm:"primaryKey" json:"id"`
	Slug        string     `json:"slug"`
	Name        string     `json:"name"`
	Plan        string     `json:"plan"`
	Status      string     `json:"status"` // active | suspended | deleting
	DataRegion  string     `json:"dataRegion"`
	Isolation   string     `json:"isolation"`
	DeleteAfter *time.Time `json:"deleteAfter,omitempty"`
	CreatedAt   time.Time  `json:"createdAt"`
	UpdatedAt   time.Time  `json:"updatedAt"`
}

type Project struct {
	ID        string    `gorm:"primaryKey" json:"id"`
	OrgID     string    `json:"org"`
	Slug      string    `json:"slug"`
	Name      string    `json:"name"`
	CreatedAt time.Time `json:"createdAt"`
}

type Environment struct {
	ID             string    `gorm:"primaryKey" json:"id"`
	OrgID          string    `json:"org"`
	Project        string    `json:"project"`
	Name           string    `json:"name"`
	TargetRef      string    `json:"targetRef"`
	PromotionOrder int       `json:"promotionOrder"`
	CreatedAt      time.Time `json:"createdAt"`
}

// Quota 는 쿼터 한도다 (FR-PLT-TEN-03).
type Quota struct {
	OrgID   string `json:"org"`
	Scope   string `gorm:"primaryKey" json:"scope"`
	ScopeID string `gorm:"primaryKey" json:"scopeId"`
	Qkey    string `gorm:"primaryKey;column:qkey" json:"key"`
	Qlimit  int    `gorm:"column:qlimit" json:"limit"`
}

func (Quota) TableName() string { return "quotas" }

// APIKey 는 개인·서비스 API 키다 (FR-PLT-IAM-06). 평문은 저장하지 않는다.
type APIKey struct {
	ID         string     `gorm:"primaryKey" json:"id"`
	OrgID      string     `json:"org"`
	Prefix     string     `json:"prefix"`
	Hash       string     `json:"-"`
	Salt       string     `json:"-"`
	Name       string     `json:"name"`
	Owner      string     `json:"owner"`
	Scopes     string     `json:"scopes"`
	ExpiresAt  time.Time  `json:"expiresAt"`
	LastUsedAt *time.Time `json:"lastUsedAt,omitempty"`
	RevokedAt  *time.Time `json:"revokedAt,omitempty"`
	CreatedAt  time.Time  `json:"createdAt"`
}

func (APIKey) TableName() string { return "api_keys" }

// AuditEvent 는 변경 불가 감사 로그 행이다 (FR-PLT-IAM-08).
type AuditEvent struct {
	ID         string    `gorm:"primaryKey" json:"id"`
	Seq        int64     `json:"seq"`
	TS         time.Time `gorm:"column:ts" json:"ts"`
	OrgID      string    `json:"org"`
	ActorType  string    `json:"actorType"`
	ActorID    string    `json:"actorId"`
	ActorIP    string    `json:"actorIp"`
	Action     string    `json:"action"`
	Resource   string    `json:"resource"`
	BeforeHash string    `json:"beforeHash"`
	AfterHash  string    `json:"afterHash"`
	Result     string    `json:"result"`
	Detail     string    `json:"detail"`
	PrevHash   string    `json:"prevHash"`
	Hash       string    `json:"hash"`
}

// ---- X-HITL · X-NTF (00006) ----

// Approval 은 승인 요청이다 (FR-X-HITL-01).
type Approval struct {
	ID             string     `gorm:"primaryKey" json:"id"`
	OrgID          string     `json:"org"`
	Project        string     `json:"project"`
	Source         string     `json:"source"`
	SourceRef      string     `json:"sourceRef"`
	AgentID        string     `json:"agentId"`
	UnitID         string     `json:"unitId"`
	Action         string     `json:"action"`
	RiskLevel      string     `json:"riskLevel"`
	Payload        string     `json:"payload"`
	PayloadHash    string     `json:"payloadHash"`
	TraceID        string     `json:"traceId"`
	Requester      string     `json:"requester"`
	Status         string     `json:"status"`
	DecisionBy     *string    `json:"decisionBy,omitempty"`
	DecisionReason *string    `json:"decisionReason,omitempty"`
	EditedPayload  *string    `json:"editedPayload,omitempty"`
	CallbackResult *string    `json:"callbackResult,omitempty"`
	ExpiresAt      time.Time  `json:"expiresAt"`
	CreatedAt      time.Time  `json:"createdAt"`
	DecidedAt      *time.Time `json:"decidedAt,omitempty"`
	// 00010: 정책 라우팅·다단 승인·SLA·실제 행위 미리보기 (FR-X-HITL-03·04·05 · GR-POL-06)
	Approvers   *string    `json:"-"`
	Quorum      int        `json:"quorum"`
	SlaSeconds  int        `json:"slaSeconds"`
	EscalateTo  *string    `json:"-"`
	EscalatedAt *time.Time `json:"escalatedAt,omitempty"`
	PolicyRule  *string    `json:"policyRule,omitempty"`
	Preview     *string    `json:"-"`
}

// ApprovalPolicy 는 조직 승인 규칙이다 (FR-X-HITL-03).
type ApprovalPolicy struct {
	OrgID     string    `gorm:"primaryKey" json:"org"`
	Policy    string    `json:"-"`
	UpdatedAt time.Time `json:"updatedAt"`
}

// ApprovalDecision 은 승인자 1인의 결정이다 (다단·2인 승인, 서명 = sha256(승인자·시각·payload 해시)).
type ApprovalDecision struct {
	ID         string    `gorm:"primaryKey" json:"id"`
	OrgID      string    `json:"org"`
	ApprovalID string    `json:"approvalId"`
	Decider    string    `json:"decider"`
	OnBehalf   *string   `json:"onBehalf,omitempty"`
	Decision   string    `json:"decision"`
	Reason     *string   `json:"reason,omitempty"`
	Signature  string    `json:"signature"`
	CreatedAt  time.Time `json:"createdAt"`
}

// ApprovalDelegation 은 부재 위임이다 (FR-X-HITL-04).
type ApprovalDelegation struct {
	OrgID     string    `gorm:"primaryKey" json:"org"`
	Principal string    `gorm:"primaryKey" json:"principal"`
	Delegate  string    `json:"delegate"`
	Until     time.Time `json:"until"`
	CreatedAt time.Time `json:"createdAt"`
}

// NotificationRule 은 알림 규칙이다 (FR-X-NTF-01).
type NotificationRule struct {
	ID        string    `gorm:"primaryKey" json:"id"`
	OrgID     string    `json:"org"`
	Name      string    `json:"name"`
	Topic     string    `json:"topic"`
	Filter    string    `json:"filter"`
	Severity  string    `json:"severity"`
	Channels  string    `json:"channels"`
	ThrottleS int       `json:"throttleS"`
	Enabled   bool      `json:"enabled"`
	CreatedAt time.Time `json:"createdAt"`
	// 00010: 중복 억제·다이제스트·에스컬레이션 (FR-X-NTF-04·05)
	DedupWindowS int     `json:"dedupWindowS"`
	DedupKeys    *string `json:"-"`
	Digest       *string `json:"digest,omitempty"`
	Escalation   *string `json:"-"`
}

// Notification 은 전달 기록이다 (FR-X-NTF-02).
type Notification struct {
	ID        string    `gorm:"primaryKey" json:"id"`
	OrgID     string    `json:"org"`
	RuleID    string    `json:"ruleId"`
	Topic     string    `json:"topic"`
	Channel   string    `json:"channel"`
	Target    string    `json:"target"`
	Status    string    `json:"status"`
	Attempts  int       `json:"attempts"`
	Error     *string   `json:"error,omitempty"`
	CreatedAt time.Time `json:"createdAt"`
	// 00010
	Fingerprint    *string    `json:"fingerprint,omitempty"`
	Payload        *string    `json:"-"`
	AckedAt        *time.Time `json:"ackedAt,omitempty"`
	AckedBy        *string    `json:"ackedBy,omitempty"`
	EscalationStep int        `json:"escalationStep"`
}

// ---- 00012 엔터프라이즈 IAM (FR-PLT-IAM-03·09 · PLT-TEN-04 · X-WSP-08) ----

// ScimUser 는 SCIM 2.0 사용자다. 권한 주체 = externalId(IdP sub) 없으면 userName.
type ScimUser struct {
	ID          string `gorm:"primaryKey"`
	OrgID       string
	ExternalID  *string
	UserName    string
	DisplayName *string
	Emails      *string
	Active      bool
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

func (u ScimUser) Subject() string {
	if u.ExternalID != nil && *u.ExternalID != "" {
		return *u.ExternalID
	}
	return u.UserName
}

type ScimGroup struct {
	ID          string `gorm:"primaryKey"`
	OrgID       string
	ExternalID  *string
	DisplayName string
	Members     string // JSON [user id]
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// ScimGroupRole 은 디렉터리 그룹 → 역할 매핑이다.
type ScimGroupRole struct {
	OrgID   string `gorm:"primaryKey" json:"org"`
	Grp     string `gorm:"primaryKey;column:grp" json:"group"`
	Role    string `gorm:"primaryKey" json:"role"`
	Project string `gorm:"primaryKey" json:"project"`
	Env     string `gorm:"primaryKey" json:"env"`
}

// OrgSecurity 는 조직 세션·MFA 정책이다 (FR-PLT-IAM-09).
type OrgSecurity struct {
	OrgID       string    `gorm:"primaryKey" json:"org"`
	MfaAdmin    bool      `json:"mfaAdmin"`
	AcrMin      int       `json:"acrMin"`
	IdleS       int       `json:"idleS"`
	MaxS        int       `json:"maxS"`
	MaxSessions int       `json:"maxSessions"`
	UpdatedAt   time.Time `json:"updatedAt"`
}

func (OrgSecurity) TableName() string { return "org_security" }

type AuthSession struct {
	Sid       string `gorm:"primaryKey"`
	OrgID     string
	Sub       string
	Revoked   bool
	CreatedAt time.Time
	LastSeen  time.Time
}

type SessionRevocation struct {
	Sub       string `gorm:"primaryKey"`
	OrgID     string
	RevokedAt time.Time
}

// Budget 은 조직 → 프로젝트 → 환경 예산이다 (FR-PLT-TEN-04). 집행은 Gateway 계층 예산.
type Budget struct {
	ID             string    `gorm:"primaryKey" json:"id"`
	OrgID          string    `json:"org"`
	Scope          string    `json:"scope"`   // org | project | env
	ScopeID        string    `json:"scopeId"` // org · org/proj · org/proj/env
	Period         string    `json:"period"`
	AmountUSD      string    `gorm:"column:amount_usd" json:"amountUsd"`
	SoftPct        int       `json:"softPct"`
	HardAction     string    `json:"hardAction"`
	DowngradeAlias *string   `json:"downgradeAlias,omitempty"`
	CreatedAt      time.Time `json:"createdAt"`
}

// Plan 은 요금제 한도다 (FR-X-WSP-08). 0 = 무제한.
type Plan struct {
	ID        string    `gorm:"primaryKey" json:"id"`
	Name      string    `json:"name"`
	Limits    string    `json:"-"`
	UpdatedAt time.Time `json:"updatedAt"`
}

type UsageMonthly struct {
	OrgID  string `gorm:"primaryKey"`
	Month  string `gorm:"primaryKey"`
	Tokens int64
}

func (UsageMonthly) TableName() string { return "usage_monthly" }

type QuotaMark struct {
	OrgID  string `gorm:"primaryKey"`
	Qkey   string `gorm:"primaryKey;column:qkey"`
	Period string `gorm:"primaryKey"`
	Level  int    `gorm:"primaryKey"`
	At     time.Time
}
