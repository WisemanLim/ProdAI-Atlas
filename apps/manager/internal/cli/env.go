// Package cli 는 atlas CLI 의 설치·백업·업그레이드 로직이다 (FR-PLT-PKG-08 · PLT-REL-08).
package cli

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"strings"
)

const alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

func signingKey() string {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return ""
	}
	der, _ := x509.MarshalECPrivateKey(k)
	return base64.StdEncoding.EncodeToString(pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}))
}

func Random(n int) string {
	b := make([]byte, n)
	for i := range b {
		x, _ := rand.Int(rand.Reader, big.NewInt(int64(len(alnum))))
		b[i] = alnum[x.Int64()]
	}
	return string(b)
}

// RenderEnv 는 .env.example 의 CHANGE_ME 를 무작위 시크릿으로 채우고 overrides 를 적용한다 (install.sh 와 같은 규칙).
func RenderEnv(example string, overrides map[string]string) string {
	key, dbpw := "sk-atlas-prod-"+Random(22), Random(32)
	var out []string
	seen := map[string]bool{}
	for _, line := range strings.Split(example, "\n") {
		k, v, ok := strings.Cut(line, "=")
		if !ok || strings.HasPrefix(strings.TrimSpace(line), "#") {
			out = append(out, line)
			continue
		}
		switch {
		case overrides[k] != "":
			v = overrides[k]
		case k == "ATLAS_GATEWAY_AL_PLAN_KEY" && v == "CHANGE_ME":
			v = key
		case k == "DB_PASSWORD" && v == "CHANGE_ME":
			v = dbpw
		case k == "MANAGER_DATABASE_URL":
			v = fmt.Sprintf("postgresql://atlas:%s@db:5432/atlas", dbpw)
		case k == "RUNTIME_DATABASE_URL":
			v = fmt.Sprintf("postgresql+psycopg://atlas:%s@db:5432/atlas", dbpw)
		case k == "OPENAI_API_KEYS" && v == "CHANGE_ME":
			v = ""
		case k == "ATLAS_MANAGER_BOOTSTRAP_KEY" && v == "CHANGE_ME": // 에이전트 서비스 키 형식 atl_{env}_{26}_{secret}
			v = "atl_prod_" + strings.ToLower(Random(26)) + "_" + Random(32)
		case k == "ATLAS_A2A_SIGNING_KEY" && v == "CHANGE_ME": // EC P-256 PEM 을 base64 한 줄로
			v = signingKey()
		case v == "CHANGE_ME":
			v = Random(32)
		}
		seen[k] = true
		out = append(out, k+"="+v)
	}
	for k, v := range overrides {
		if !seen[k] {
			out = append(out, k+"="+v)
		}
	}
	return strings.Join(out, "\n")
}

// ReadEnv 는 KEY=VALUE 파일을 읽는다.
func ReadEnv(path string) (map[string]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		if k, v, ok := strings.Cut(line, "="); ok && !strings.HasPrefix(strings.TrimSpace(line), "#") {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return out, nil
}

// SetEnv 는 파일의 KEY 값을 바꾼다(없으면 추가).
func SetEnv(path, key, value string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	lines := strings.Split(string(b), "\n")
	found := false
	for i, l := range lines {
		if strings.HasPrefix(l, key+"=") {
			lines[i], found = key+"="+value, true
		}
	}
	if !found {
		lines = append(lines, key+"="+value)
	}
	return os.WriteFile(path, []byte(strings.Join(lines, "\n")), 0o600)
}

// LoadCompat 는 compat.json 파일을 읽는다.
func LoadCompat(path string) (map[string]any, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal(b, &out)
}
