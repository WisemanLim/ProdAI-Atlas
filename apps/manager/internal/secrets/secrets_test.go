package secrets

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeKV 는 OpenBao KV v2 최소 흉내다(data GET/POST/DELETE, metadata LIST, 토큰 검사).
func fakeKV(t *testing.T, token string) *httptest.Server {
	type ver struct {
		v       string
		deleted bool
	}
	var mu sync.Mutex
	store := map[string][]ver{}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Vault-Token") != token {
			http.Error(w, `{"errors":["permission denied"]}`, http.StatusForbidden)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		p := strings.TrimPrefix(r.URL.Path, "/v1/atlas/")
		switch {
		case strings.HasPrefix(p, "data/"):
			k := strings.TrimPrefix(p, "data/")
			switch r.Method {
			case http.MethodPost:
				var body struct{ Data map[string]string }
				_ = json.NewDecoder(r.Body).Decode(&body)
				store[k] = append(store[k], ver{v: body.Data["value"]})
				_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"version": len(store[k]), "created_time": time.Now()}})
			case http.MethodGet:
				vs := store[k]
				if len(vs) == 0 {
					w.WriteHeader(404)
					return
				}
				last := vs[len(vs)-1]
				data := map[string]string{"value": last.v}
				if last.deleted {
					data = nil
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"data": data, "metadata": map[string]any{"version": len(vs), "created_time": time.Now()}}})
			case http.MethodDelete:
				if vs := store[k]; len(vs) > 0 {
					vs[len(vs)-1].deleted = true
				}
				w.WriteHeader(204)
			}
		case strings.HasPrefix(p, "metadata/") && r.Method == "LIST":
			dir := strings.TrimPrefix(p, "metadata/")
			seen := map[string]bool{}
			var keys []string
			for k := range store {
				if rest, ok := strings.CutPrefix(k, dir); ok {
					seg, _, more := strings.Cut(rest, "/")
					if more {
						seg += "/"
					}
					if !seen[seg] {
						seen[seg], keys = true, append(keys, seg)
					}
				}
			}
			if len(keys) == 0 {
				w.WriteHeader(404)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"keys": keys}})
		default:
			w.WriteHeader(400)
		}
	}))
}

func exercise(t *testing.T, s Store) {
	t.Helper()
	ctx := context.Background()
	if _, err := s.Get(ctx, "orgs/acme/db"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing: %v", err)
	}
	m1, err := s.Put(ctx, "orgs/acme/db", "s3cret-1")
	if err != nil || m1.Version != 1 {
		t.Fatalf("put: %+v %v", m1, err)
	}
	prev, cur, err := s.Rotate(ctx, "orgs/acme/db", "s3cret-2")
	if err != nil || prev.Version != 1 || cur.Version != 2 {
		t.Fatalf("rotate: %+v %+v %v", prev, cur, err)
	}
	got, _ := s.Get(ctx, "orgs/acme/db")
	if got.Value != "s3cret-2" || got.Version != 2 {
		t.Fatalf("get: %+v", got)
	}
	_, _ = s.Put(ctx, "orgs/acme/api", "x")
	_, _ = s.Put(ctx, "orgs/beta/db", "y")
	l, _ := s.List(ctx, "orgs/acme")
	if len(l) != 2 {
		t.Fatalf("list acme: %+v", l)
	}
	b, _ := json.Marshal(l)
	if strings.Contains(string(b), "s3cret") {
		t.Fatal("list leaks values")
	}
	if err := s.Delete(ctx, "orgs/acme/db"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Get(ctx, "orgs/acme/db"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted: %v", err)
	}
	if _, err := s.Put(ctx, "../etc/passwd", "x"); !errors.Is(err, ErrBadPath) {
		t.Fatalf("path traversal accepted: %v", err)
	}
}

func TestFileStore(t *testing.T) {
	dir := t.TempDir()
	s, err := Open("file://" + dir)
	if err != nil {
		t.Fatal(err)
	}
	exercise(t, s)
	st, _ := os.Stat(filepath.Join(dir, "orgs/acme/api/v1"))
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("file mode: %v", st.Mode())
	}
}

func TestBaoStore(t *testing.T) {
	srv := fakeKV(t, "tok-1")
	defer srv.Close()
	s, err := Open("bao://" + strings.TrimPrefix(srv.URL, "http://") + "/atlas?token=tok-1")
	if err != nil {
		t.Fatal(err)
	}
	exercise(t, s)
	bad, _ := Open("bao://" + strings.TrimPrefix(srv.URL, "http://") + "/atlas?token=wrong")
	if _, err := bad.Put(context.Background(), "x", "y"); err == nil || strings.Contains(err.Error(), "y") && strings.Contains(err.Error(), "value") {
		t.Fatalf("wrong token must fail without echoing value: %v", err)
	}
}
