package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// 공급사 Batch API 어댑터 (FR-EC-BATCH-01·02). 게이트웨이는 OpenAI 형식 JSONL 을 받아 행마다 Request 로 바꾸고,
// 공급자별 배치로 제출한 뒤 폴링해 결과를 모은다.

type BatchRow struct {
	CustomID string
	Req      Request
}

type BatchResult struct {
	CustomID string    `json:"custom_id"`
	Status   int       `json:"status_code"`
	Response *Response `json:"-"`
	Error    string    `json:"error,omitempty"`
}

// 배치 상태 (공급자 상태를 이 다섯으로 정규화)
const (
	BatchValidating = "validating"
	BatchInProgress = "in_progress"
	BatchEnded      = "ended"
	BatchFailed     = "failed"
	BatchExpired    = "expired"
)

type BatchProvider interface {
	SubmitBatch(ctx context.Context, rows []BatchRow) (string, error)
	// PollBatch 는 정규화 상태와, 끝났으면 행별 결과를 돌려준다.
	PollBatch(ctx context.Context, id string) (string, []BatchResult, error)
}

// MockBatch 는 Mock 공급자의 배치다: Delay 가 지나면 행마다 Mock.Complete 를 돌려 끝낸다.
// 마지막 user 메시지에 "batch-fail" 이 있으면 그 행은 500 으로 실패한다 (재제출 시험용).
type MockBatch struct {
	Mock  Mock
	Delay time.Duration
	mu    sync.Mutex
	jobs  map[string]mockJob
}

type mockJob struct {
	rows []BatchRow
	at   time.Time
}

func (m *MockBatch) SubmitBatch(_ context.Context, rows []BatchRow) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.jobs == nil {
		m.jobs = map[string]mockJob{}
	}
	id := "mockbatch_" + uuid.NewString()
	m.jobs[id] = mockJob{rows: rows, at: time.Now()}
	return id, nil
}

func (m *MockBatch) PollBatch(ctx context.Context, id string) (string, []BatchResult, error) {
	m.mu.Lock()
	job, ok := m.jobs[id]
	m.mu.Unlock()
	if !ok {
		return BatchFailed, nil, fmt.Errorf("mock batch %s 없음", id)
	}
	if time.Since(job.at) < m.Delay {
		return BatchInProgress, nil, nil
	}
	out := make([]BatchResult, 0, len(job.rows))
	for _, r := range job.rows {
		if n := len(r.Req.Messages); n > 0 && strings.Contains(r.Req.Messages[n-1].Content, "batch-fail") {
			out = append(out, BatchResult{CustomID: r.CustomID, Status: 500, Error: "mock row failure"})
			continue
		}
		resp, err := m.Mock.Complete(ctx, r.Req)
		if err != nil {
			out = append(out, BatchResult{CustomID: r.CustomID, Status: 502, Error: err.Error()})
			continue
		}
		out = append(out, BatchResult{CustomID: r.CustomID, Status: 200, Response: &resp})
	}
	m.mu.Lock()
	delete(m.jobs, id)
	m.mu.Unlock()
	return BatchEnded, out, nil
}

// SubmitBatch 는 OpenAI files.create(purpose=batch) + batches.create(completion_window=24h) 다.
func (o OpenAI) SubmitBatch(ctx context.Context, rows []BatchRow) (string, error) {
	var jsonl bytes.Buffer
	for _, r := range rows {
		body := map[string]any{"model": r.Req.Model, "messages": r.Req.Messages}
		if r.Req.MaxTokens > 0 {
			body["max_tokens"] = r.Req.MaxTokens
		}
		if r.Req.Temperature != nil {
			body["temperature"] = *r.Req.Temperature
		}
		line, _ := json.Marshal(map[string]any{"custom_id": r.CustomID, "method": "POST", "url": "/v1/chat/completions", "body": body})
		jsonl.Write(append(line, '\n'))
	}
	var form bytes.Buffer
	mw := multipart.NewWriter(&form)
	_ = mw.WriteField("purpose", "batch")
	fw, _ := mw.CreateFormFile("file", "atlas-batch.jsonl")
	_, _ = fw.Write(jsonl.Bytes())
	_ = mw.Close()
	var file struct {
		ID string `json:"id"`
	}
	if err := o.call(ctx, http.MethodPost, "/files", mw.FormDataContentType(), &form, &file); err != nil {
		return "", err
	}
	b, _ := json.Marshal(map[string]string{"input_file_id": file.ID, "endpoint": "/v1/chat/completions", "completion_window": "24h"})
	var batch struct {
		ID string `json:"id"`
	}
	if err := o.call(ctx, http.MethodPost, "/batches", "application/json", bytes.NewReader(b), &batch); err != nil {
		return "", err
	}
	return batch.ID, nil
}

func (o OpenAI) PollBatch(ctx context.Context, id string) (string, []BatchResult, error) {
	var b struct {
		Status       string `json:"status"`
		OutputFileID string `json:"output_file_id"`
		ErrorFileID  string `json:"error_file_id"`
	}
	if err := o.call(ctx, http.MethodGet, "/batches/"+id, "", nil, &b); err != nil {
		return "", nil, err
	}
	switch b.Status {
	case "validating":
		return BatchValidating, nil, nil
	case "in_progress", "finalizing", "cancelling":
		return BatchInProgress, nil, nil
	case "failed", "cancelled":
		return BatchFailed, nil, nil
	case "expired":
		return BatchExpired, nil, nil
	}
	var out []BatchResult
	for _, fid := range []string{b.OutputFileID, b.ErrorFileID} {
		if fid == "" {
			continue
		}
		var raw bytes.Buffer
		if err := o.call(ctx, http.MethodGet, "/files/"+fid+"/content", "", nil, &raw); err != nil {
			return "", nil, err
		}
		for _, line := range bytes.Split(raw.Bytes(), []byte("\n")) {
			if len(bytes.TrimSpace(line)) == 0 {
				continue
			}
			var l struct {
				CustomID string `json:"custom_id"`
				Response *struct {
					StatusCode int `json:"status_code"`
					Body       struct {
						Choices []struct {
							Message      Message `json:"message"`
							FinishReason string  `json:"finish_reason"`
						} `json:"choices"`
						Usage struct {
							Prompt     int `json:"prompt_tokens"`
							Completion int `json:"completion_tokens"`
						} `json:"usage"`
					} `json:"body"`
				} `json:"response"`
				Error *struct {
					Message string `json:"message"`
				} `json:"error"`
			}
			if json.Unmarshal(line, &l) != nil {
				continue
			}
			r := BatchResult{CustomID: l.CustomID}
			switch {
			case l.Error != nil:
				r.Status, r.Error = 500, l.Error.Message
			case l.Response != nil && l.Response.StatusCode == 200 && len(l.Response.Body.Choices) > 0:
				c := l.Response.Body.Choices[0]
				r.Status = 200
				r.Response = &Response{Content: c.Message.Content, ToolCalls: c.Message.ToolCalls, FinishReason: c.FinishReason,
					Usage: Usage{Prompt: l.Response.Body.Usage.Prompt, Completion: l.Response.Body.Usage.Completion}}
			case l.Response != nil:
				r.Status, r.Error = l.Response.StatusCode, "upstream row error"
			}
			out = append(out, r)
		}
	}
	return BatchEnded, out, nil
}

// call 은 업스트림 JSON/바이너리 호출이다. out 이 *bytes.Buffer 면 본문을 그대로 담는다.
func (o OpenAI) call(ctx context.Context, method, path, ctype string, body io.Reader, out any) error {
	key, idx, ok := o.Pool.Pick()
	if !ok {
		return &Error{Status: http.StatusServiceUnavailable, Msg: "사용 가능한 업스트림 키 없음"}
	}
	hr, _ := http.NewRequestWithContext(ctx, method, o.BaseURL+path, body)
	if ctype != "" {
		hr.Header.Set("Content-Type", ctype)
	}
	hr.Header.Set("Authorization", "Bearer "+key)
	res, err := o.Client.Do(hr)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 256<<20))
	o.Pool.Report(idx, res.StatusCode)
	if res.StatusCode >= 300 {
		return &Error{Status: res.StatusCode, Msg: string(raw[:min(len(raw), 300)])}
	}
	if buf, ok := out.(*bytes.Buffer); ok {
		buf.Write(raw)
		return nil
	}
	return json.Unmarshal(raw, out)
}
