// Package manifest 는 atlas-agent.yaml(JSON 표현, 스키마 v1alpha1 — PRD §11.14)을 검증하고 리비전 해시를 계산한다.
package manifest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"atlas-manager/internal/catalog"
)

const (
	APIVersion = "agents.prodai.io/v1alpha1"
	Kind       = "AtlasAgent"
)

var (
	nameRe    = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)
	semverRe  = regexp.MustCompile(`^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$`)
	bindings  = map[string]bool{"JSONRPC": true, "GRPC": true, "HTTP+JSON": true}
	drivers   = map[string]bool{"local": true, "compose": true, "kubernetes": true, "serverless": true}
	digestRef = regexp.MustCompile(`@sha256:[0-9a-f]{64}$`)
)

// Manifest 는 검증에 필요한 필드만 구조화한다. 원문은 Raw 로 보존해 그대로 저장한다.
type Manifest struct {
	APIVersion string         `json:"apiVersion"`
	Kind       string         `json:"kind"`
	Metadata   Metadata       `json:"metadata"`
	Spec       Spec           `json:"spec"`
	Raw        map[string]any `json:"-"`
}

type Metadata struct {
	Name    string            `json:"name"`
	UnitID  string            `json:"unitId"`
	Layer   string            `json:"layer,omitempty"`
	Concept string            `json:"concept,omitempty"`
	Labels  map[string]string `json:"labels,omitempty"`
}

type Spec struct {
	Version   string `json:"version"`
	Artifacts struct {
		Image string `json:"image,omitempty"`
		Wheel string `json:"wheel,omitempty"`
	} `json:"artifacts"`
	Interfaces []struct {
		Binding string `json:"binding"`
		Port    int    `json:"port"`
		Path    string `json:"path,omitempty"`
	} `json:"interfaces"`
	Drivers struct {
		Supported []string `json:"supported,omitempty"`
	} `json:"drivers"`
	// Secrets 는 에이전트가 읽을 비밀 선언이다 — 선언한 이름만 Manager 비밀 중개로 받는다 (FR-PLT-SEC-08 최소 권한).
	Secrets []struct {
		Name     string `json:"name"`
		Required bool   `json:"required,omitempty"`
	} `json:"secrets,omitempty"`
}

var secretNameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,62}$`)

// SecretNames 는 선언된 비밀 이름 집합이다.
func (m *Manifest) SecretNames() map[string]bool {
	out := map[string]bool{}
	for _, s := range m.Spec.Secrets {
		out[s.Name] = true
	}
	return out
}

// FieldError 는 매니페스트 필드 단위 검증 오류다.
type FieldError struct {
	Field   string `json:"field"`
	Message string `json:"message"`
}

// Errors 는 하나 이상의 FieldError 를 담는 error 다.
type Errors []FieldError

func (e Errors) Error() string {
	parts := make([]string, len(e))
	for i, fe := range e {
		parts[i] = fe.Field + ": " + fe.Message
	}
	return "invalid manifest: " + strings.Join(parts, "; ")
}

// Parse 는 JSON 본문을 읽어 검증한다. metadata.layer/concept 가 비어 있으면 카탈로그 값으로 채운다.
func Parse(body []byte) (*Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, Errors{{Field: "$", Message: "JSON 파싱 실패: " + err.Error()}}
	}
	if err := json.Unmarshal(body, &m.Raw); err != nil {
		return nil, Errors{{Field: "$", Message: "JSON 객체여야 함"}}
	}
	if errs := m.validate(); len(errs) > 0 {
		return nil, errs
	}
	u, _ := catalog.Get(m.Metadata.UnitID)
	md, _ := m.Raw["metadata"].(map[string]any)
	if m.Metadata.Layer == "" {
		m.Metadata.Layer, md["layer"] = u.Layer, u.Layer
	}
	if m.Metadata.Concept == "" {
		m.Metadata.Concept, md["concept"] = u.Concept, u.Concept
	}
	return &m, nil
}

func (m *Manifest) validate() Errors {
	var errs Errors
	add := func(f, msg string, a ...any) { errs = append(errs, FieldError{f, fmt.Sprintf(msg, a...)}) }

	if m.APIVersion != APIVersion {
		add("apiVersion", "%q 이어야 함", APIVersion)
	}
	if m.Kind != Kind {
		add("kind", "%q 이어야 함", Kind)
	}
	if !nameRe.MatchString(m.Metadata.Name) {
		add("metadata.name", "DNS label(소문자·숫자·'-', 63자 이하)이어야 함")
	}
	if u, ok := catalog.Get(m.Metadata.UnitID); !ok {
		add("metadata.unitId", "카탈로그에 없는 단위 %q (GET /api/v1/catalog/units)", m.Metadata.UnitID)
	} else {
		if m.Metadata.Layer != "" && m.Metadata.Layer != u.Layer {
			add("metadata.layer", "%s 의 레이어는 %s", u.ID, u.Layer)
		}
		if m.Metadata.Concept != "" && m.Metadata.Concept != u.Concept {
			add("metadata.concept", "%s 의 개념은 %s", u.ID, u.Concept)
		}
	}
	if _, ok := m.Raw["spec"].(map[string]any); !ok {
		add("spec", "필수")
		return errs
	}
	if !semverRe.MatchString(m.Spec.Version) {
		add("spec.version", "SemVer 여야 함")
	}
	if img := m.Spec.Artifacts.Image; img != "" && !digestRef.MatchString(img) {
		add("spec.artifacts.image", "digest 고정(@sha256:<64hex>) 필수 (FR-PLT-MGR-01)")
	}
	if len(m.Spec.Interfaces) == 0 {
		add("spec.interfaces", "1개 이상 필요")
	}
	for i, it := range m.Spec.Interfaces {
		if !bindings[it.Binding] {
			add(fmt.Sprintf("spec.interfaces[%d].binding", i), "JSONRPC | GRPC | HTTP+JSON")
		}
		if it.Port < 1 || it.Port > 65535 {
			add(fmt.Sprintf("spec.interfaces[%d].port", i), "1~65535")
		}
	}
	for i, d := range m.Spec.Drivers.Supported {
		if !drivers[d] {
			add(fmt.Sprintf("spec.drivers.supported[%d]", i), "local | compose | kubernetes | serverless")
		}
	}
	seen := map[string]bool{}
	for i, s := range m.Spec.Secrets {
		if !secretNameRe.MatchString(s.Name) || seen[s.Name] {
			add(fmt.Sprintf("spec.secrets[%d].name", i), "소문자·숫자·._- 1~63자, 중복 불가")
		}
		seen[s.Name] = true
	}
	return errs
}

// SpecHash 는 spec 의 SHA-256 이다. encoding/json 은 맵 키를 정렬하므로 같은 spec 은 같은 해시를 낸다
// (JCS RFC 8785 근사 — 숫자 표기 정규화는 M1 서명 도입 시 엄밀화).
func (m *Manifest) SpecHash() string {
	b, _ := json.Marshal(m.Raw["spec"])
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// JSON 은 저장용 원문 JSON 이다.
func (m *Manifest) JSON() string {
	b, _ := json.Marshal(m.Raw)
	return string(b)
}
