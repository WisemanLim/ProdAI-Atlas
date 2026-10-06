// Package events 는 NATS JetStream 에 CloudEvents 1.0 envelope 로 이벤트를 발행한다(PRD §14.4 · §17.3).
package events

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// Event 는 CloudEvents 1.0 구조화 모드 envelope 이다.
type Event struct {
	SpecVersion     string    `json:"specversion"`
	ID              string    `json:"id"`
	Source          string    `json:"source"`
	Type            string    `json:"type"`
	Time            time.Time `json:"time"`
	DataContentType string    `json:"datacontenttype"`
	Data            any       `json:"data"`
}

// Publisher 는 subject(= 이벤트 type)로 data 를 발행한다.
type Publisher interface {
	Publish(ctx context.Context, subject string, data any) error
}

// New 는 CloudEvents envelope 를 만든다.
func New(source, subject string, data any) Event {
	return Event{"1.0", uuid.NewString(), source, subject, time.Now().UTC(), "application/json", data}
}

// Nop 은 NATS_URL 이 없을 때 쓰는 무동작 발행기다.
type Nop struct{}

func (Nop) Publish(context.Context, string, any) error { return nil }

// Recorder 는 시험용 발행기다.
type Recorder struct {
	mu     sync.Mutex
	Events []Event
}

func (r *Recorder) Publish(_ context.Context, subject string, data any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.Events = append(r.Events, New("test", subject, data))
	return nil
}

func (r *Recorder) Types() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.Events))
	for i, e := range r.Events {
		out[i] = e.Type
	}
	return out
}

// JetStream 은 NATS JetStream 발행기다. 스트림이 없으면 만든다.
type JetStream struct {
	source string
	nc     *nats.Conn
	js     jetstream.JetStream
}

func Connect(ctx context.Context, url, source, stream string, subjects []string) (*JetStream, error) {
	nc, err := nats.Connect(url, nats.Name(source), nats.MaxReconnects(-1))
	if err != nil {
		return nil, err
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, err
	}
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{Name: stream, Subjects: subjects, MaxAge: 7 * 24 * time.Hour}); err != nil {
		nc.Close()
		return nil, err
	}
	return &JetStream{source, nc, js}, nil
}

func (j *JetStream) Publish(ctx context.Context, subject string, data any) error {
	b, err := json.Marshal(New(j.source, subject, data))
	if err != nil {
		return err
	}
	_, err = j.js.Publish(ctx, subject, b)
	return err
}

func (j *JetStream) Close() { j.nc.Close() }
