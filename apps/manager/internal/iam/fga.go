package iam

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// FGA 는 OpenFGA HTTP 클라이언트다 (FR-PLT-IAM-04 RBAC). 스토어 "atlas" 를 찾거나 만들고, 기동 시 모델을 쓴다.
type FGA struct {
	URL     string
	Token   string // OPENFGA_AUTHN_PRESHARED_KEYS
	Client  *http.Client
	mu      sync.RWMutex
	storeID string
	modelID string
}

// Roles 는 역할 템플릿 → (객체 타입, relation) 이다. 객체 id 는 조직 slug · org/project · org/project/env.
var Roles = map[string][2]string{
	"platform-admin":  {"platform", "admin"},
	"org-owner":       {"organization", "owner"},
	"org-admin":       {"organization", "admin"},
	"billing":         {"organization", "billing"},
	"auditor":         {"organization", "auditor"},
	"viewer":          {"organization", "viewer"},
	"project-admin":   {"project", "admin"},
	"agent-developer": {"project", "developer"},
	"deployer":        {"project", "deployer"},
	"operator":        {"project", "operator"},
	"env-deployer":    {"environment", "deployer"},
}

// Model 은 OpenFGA 권한 모델(schema 1.1)이다. DSL 요지:
//
//	organization: admin = [user, group#member] or owner or admin from platform; viewer = [...] or member
//	project:      admin = [...] or admin from org; viewer = [...] or developer or deployer or operator or viewer from org
//	environment:  deploy = deployer; deployer = [...] or deployer from project
func Model() map[string]any {
	this := map[string]any{"this": map[string]any{}}
	cu := func(rel string) map[string]any {
		return map[string]any{"computedUserset": map[string]any{"relation": rel}}
	}
	ttu := func(tupleset, rel string) map[string]any {
		return map[string]any{"tupleToUserset": map[string]any{"tupleset": map[string]any{"relation": tupleset}, "computedUserset": map[string]any{"relation": rel}}}
	}
	union := func(c ...map[string]any) map[string]any { return map[string]any{"union": map[string]any{"child": c}} }
	direct := func(types ...string) map[string]any {
		var refs []map[string]any
		for _, t := range types {
			if name, cond, ok := strings.Cut(t, " with "); ok { // 조건부 직접 관계 (ABAC, FR-PLT-IAM-05)
				refs = append(refs, map[string]any{"type": name, "condition": cond})
			} else if name, rel, ok := strings.Cut(t, "#"); ok {
				refs = append(refs, map[string]any{"type": name, "relation": rel})
			} else {
				refs = append(refs, map[string]any{"type": t})
			}
		}
		return map[string]any{"directly_related_user_types": refs}
	}
	u := direct("user", "group#member")
	return map[string]any{"schema_version": "1.1", "conditions": Conditions(), "type_definitions": []any{
		map[string]any{"type": "user"},
		map[string]any{"type": "group", "relations": map[string]any{"member": this}, "metadata": map[string]any{"relations": map[string]any{"member": direct("user")}}},
		map[string]any{"type": "platform", "relations": map[string]any{"admin": this}, "metadata": map[string]any{"relations": map[string]any{"admin": direct("user")}}},
		map[string]any{"type": "organization",
			"relations": map[string]any{
				"platform": this,
				"owner":    this,
				"admin":    union(this, cu("owner"), ttu("platform", "admin")),
				"billing":  union(this, cu("admin")),
				"auditor":  union(this, cu("admin")),
				"member":   union(this, cu("admin"), cu("billing"), cu("auditor")),
				"viewer":   union(this, cu("member")),
			},
			"metadata": map[string]any{"relations": map[string]any{"platform": direct("platform"), "owner": direct("user"), "admin": u,
				"billing": direct("user", "group#member", "user with amount_limit", "user with business_hours"), "auditor": u, "member": u, "viewer": u}}},
		map[string]any{"type": "project",
			"relations": map[string]any{
				"org":       this,
				"admin":     union(this, ttu("org", "admin")),
				"developer": union(this, cu("admin")),
				"deployer":  union(this, cu("admin")),
				"operator":  union(this, cu("admin")),
				"viewer":    union(this, cu("developer"), cu("deployer"), cu("operator"), ttu("org", "viewer")),
				"invoke":    union(cu("developer"), cu("operator"), cu("deployer")),
			},
			"metadata": map[string]any{"relations": map[string]any{"org": direct("organization"), "admin": u, "developer": u,
				"deployer": u, "operator": u, "viewer": u}}},
		map[string]any{"type": "environment",
			"relations": map[string]any{
				"project":  this,
				"deployer": union(this, ttu("project", "deployer")),
				"deploy":   cu("deployer"),
			},
			"metadata": map[string]any{"relations": map[string]any{"project": direct("project"),
				"deployer": direct("user", "group#member", "user with business_hours", "user with ip_allowlist", "user with mfa_verified")}}},
	}}
}

// Conditions 는 ABAC 조건(CEL)이다 (FR-PLT-IAM-05). 튜플이 매개변수 일부(tz·시간대·cidr·한도)를, 요청이 나머지(now·ip·mfa·amount)를 준다.
func Conditions() map[string]any {
	p := func(kv ...string) map[string]any {
		out := map[string]any{}
		for i := 0; i < len(kv); i += 2 {
			out[kv[i]] = map[string]any{"type_name": kv[i+1]}
		}
		return out
	}
	return map[string]any{
		"business_hours": map[string]any{"name": "business_hours", "expression": "now.getHours(tz) >= start_hour && now.getHours(tz) < end_hour",
			"parameters": p("now", "TYPE_NAME_TIMESTAMP", "tz", "TYPE_NAME_STRING", "start_hour", "TYPE_NAME_INT", "end_hour", "TYPE_NAME_INT")},
		"ip_allowlist": map[string]any{"name": "ip_allowlist", "expression": "ip.in_cidr(cidr)",
			"parameters": p("ip", "TYPE_NAME_IPADDRESS", "cidr", "TYPE_NAME_STRING")},
		"mfa_verified": map[string]any{"name": "mfa_verified", "expression": "mfa == true", "parameters": p("mfa", "TYPE_NAME_BOOL")},
		"amount_limit": map[string]any{"name": "amount_limit", "expression": "amount <= max_amount",
			"parameters": p("amount", "TYPE_NAME_DOUBLE", "max_amount", "TYPE_NAME_DOUBLE")},
	}
}

func (f *FGA) call(ctx context.Context, method, path string, body, out any) error {
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, _ := http.NewRequestWithContext(ctx, method, f.URL+path, r)
	req.Header.Set("Content-Type", "application/json")
	if f.Token != "" {
		req.Header.Set("Authorization", "Bearer "+f.Token)
	}
	res, err := f.Client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 4<<20))
	if res.StatusCode >= 300 {
		return fmt.Errorf("openfga %s %s: %d %s", method, path, res.StatusCode, raw[:min(len(raw), 300)])
	}
	if out != nil {
		return json.Unmarshal(raw, out)
	}
	return nil
}

// Bootstrap 은 스토어를 찾거나 만들고 모델을 쓴다(재시도 포함 — compose 기동 순서 대비).
func (f *FGA) Bootstrap(ctx context.Context) error {
	var last error
	for i := 0; i < 60; i++ {
		if last = f.bootstrap(ctx); last == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return last
}

func (f *FGA) bootstrap(ctx context.Context) error {
	var stores struct {
		Stores []struct{ ID, Name string } `json:"stores"`
	}
	if err := f.call(ctx, http.MethodGet, "/stores?page_size=100", nil, &stores); err != nil {
		return err
	}
	id := ""
	for _, s := range stores.Stores {
		if s.Name == "atlas" {
			id = s.ID
		}
	}
	if id == "" {
		var created struct{ ID string }
		if err := f.call(ctx, http.MethodPost, "/stores", map[string]string{"name": "atlas"}, &created); err != nil {
			return err
		}
		id = created.ID
	}
	var m struct {
		AuthorizationModelID string `json:"authorization_model_id"`
	}
	if err := f.call(ctx, http.MethodPost, "/stores/"+id+"/authorization-models", Model(), &m); err != nil {
		return err
	}
	f.mu.Lock()
	f.storeID, f.modelID = id, m.AuthorizationModelID
	f.mu.Unlock()
	return nil
}

func (f *FGA) ids() (string, string, error) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	if f.storeID == "" {
		return "", "", errors.New("openfga 미초기화")
	}
	return f.storeID, f.modelID, nil
}

type Tuple struct {
	User      string        `json:"user"`
	Relation  string        `json:"relation"`
	Object    string        `json:"object"`
	Condition *RelCondition `json:"condition,omitempty"`
}

// RelCondition 은 튜플 조건(이름 + 저장 문맥)이다.
type RelCondition struct {
	Name    string         `json:"name"`
	Context map[string]any `json:"context,omitempty"`
}

// Write 는 튜플을 쓴다(이미 있는 튜플은 무시하려고 하나씩 쓴다).
func (f *FGA) Write(ctx context.Context, writes, deletes []Tuple) error {
	sid, mid, err := f.ids()
	if err != nil {
		return err
	}
	for _, t := range writes {
		err := f.call(ctx, http.MethodPost, "/stores/"+sid+"/write", map[string]any{"authorization_model_id": mid,
			"writes": map[string]any{"tuple_keys": []Tuple{t}, "on_duplicate": "ignore"}}, nil)
		if err != nil {
			return err
		}
	}
	for _, t := range deletes {
		t.Condition = nil // 삭제 키에는 조건이 없다
		err := f.call(ctx, http.MethodPost, "/stores/"+sid+"/write", map[string]any{"authorization_model_id": mid,
			"deletes": map[string]any{"tuple_keys": []Tuple{t}, "on_missing": "ignore"}}, nil)
		if err != nil {
			return err
		}
	}
	return nil
}

func (f *FGA) Check(ctx context.Context, user, relation, object string) (bool, error) {
	return f.CheckCtx(ctx, user, relation, object, nil)
}

// CheckCtx 는 요청 문맥(조건 매개변수)을 실어 검사한다.
func (f *FGA) CheckCtx(ctx context.Context, user, relation, object string, reqCtx map[string]any) (bool, error) {
	sid, mid, err := f.ids()
	if err != nil {
		return false, err
	}
	var out struct {
		Allowed bool `json:"allowed"`
	}
	body := map[string]any{"authorization_model_id": mid, "tuple_key": Tuple{User: user, Relation: relation, Object: object}}
	if len(reqCtx) > 0 {
		body["context"] = reqCtx
	}
	err = f.call(ctx, http.MethodPost, "/stores/"+sid+"/check", body, &out)
	return out.Allowed, err
}

// Read 는 객체의 직접 튜플을 읽는다(멤버 목록).
func (f *FGA) Read(ctx context.Context, object string) ([]Tuple, error) {
	sid, _, err := f.ids()
	if err != nil {
		return nil, err
	}
	var out struct {
		Tuples []struct {
			Key Tuple `json:"key"`
		} `json:"tuples"`
	}
	if err := f.call(ctx, http.MethodPost, "/stores/"+sid+"/read", map[string]any{"tuple_key": map[string]string{"object": object}, "page_size": 100}, &out); err != nil {
		return nil, err
	}
	ts := make([]Tuple, 0, len(out.Tuples))
	for _, t := range out.Tuples {
		ts = append(ts, t.Key)
	}
	return ts, nil
}

// Structure 는 조직·프로젝트·환경의 부모 연결 튜플이다.
func Structure(org, project string, envs []string) []Tuple {
	ts := []Tuple{{User: "platform:atlas", Relation: "platform", Object: "organization:" + org}}
	if project != "" {
		p := "project:" + org + "/" + project
		ts = append(ts, Tuple{User: "organization:" + org, Relation: "org", Object: p})
		for _, e := range envs {
			ts = append(ts, Tuple{User: p, Relation: "project", Object: "environment:" + org + "/" + project + "/" + e})
		}
	}
	return ts
}
