package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"deploy-platform.local/agentapi"
	"deploy-platform.local/controlplane/control"
)

const testToken = "01234567890123456789012345678901"

type fakePort struct {
	calls   int
	last    agentapi.Submission
	failure error
}

func (f *fakePort) Health(context.Context) (agentapi.Health, error) {
	return agentapi.Health{APIVersion: "v1", Accepting: true}, f.failure
}
func (f *fakePort) Applications(context.Context) ([]agentapi.Application, error) {
	return []agentapi.Application{}, f.failure
}
func (f *fakePort) Operations(context.Context, uint16) ([]agentapi.Operation, error) {
	return []agentapi.Operation{}, f.failure
}
func (f *fakePort) Job(_ context.Context, id int64) (agentapi.Job, error) {
	return agentapi.Job{ID: id, RequestID: "key", State: "queued"}, f.failure
}
func (f *fakePort) Submit(_ context.Context, value agentapi.Submission) (agentapi.Job, error) {
	f.calls++
	f.last = value
	return agentapi.Job{ID: 7, RequestID: value.RequestID, State: "queued"}, f.failure
}
func handler(t *testing.T, f *fakePort) http.Handler {
	t.Helper()
	h, err := New(control.New(f), testToken)
	if err != nil {
		t.Fatal(err)
	}
	return h
}
func request(h http.Handler, method, path, body, token string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	out := httptest.NewRecorder()
	h.ServeHTTP(out, req)
	return out
}

const applyBody = `{"request_id":"deploy-1","spec":{"schema_version":1,"app":{"project":"demo","name":"web"},"image":"example/web:v1","resources":{"memory_mib":128,"cpu_millis":500,"pids":64}}}`

func TestApplyAndIntentUseStableKeysAndAsyncJobs(t *testing.T) {
	f := &fakePort{}
	h := handler(t, f)
	out := request(h, "POST", "/api/v1/apps/apply", applyBody, testToken)
	if out.Code != 202 || f.last.RequestID != "deploy-1" || f.last.Command.Kind != "apply" || *f.last.Command.StartupWaitMS != 1000 {
		t.Fatalf("bad apply: %d %s", out.Code, out.Body.String())
	}
	for _, action := range []string{"start", "stop"} {
		out = request(h, "POST", "/api/v1/apps/demo/web/"+action, `{"request_id":"intent-1"}`, testToken)
		if out.Code != 202 || f.last.Command.Kind != action || f.last.Command.App.Name != "web" {
			t.Fatalf("bad intent: %d", out.Code)
		}
	}
	for _, path := range []string{"/api/v1/apps", "/api/v1/health", "/api/v1/jobs/7", "/api/v1/operations?limit=10"} {
		out = request(h, "GET", path, "", testToken)
		if out.Code != 200 {
			t.Fatalf("%s: %d", path, out.Code)
		}
	}
}
func TestAuthValidationAndLimitsNeverForwardInvalidMutation(t *testing.T) {
	f := &fakePort{}
	h := handler(t, f)
	for _, test := range []struct {
		method, path, body, token string
		status                    int
	}{
		{"POST", "/api/v1/apps/apply", applyBody, "", 401},
		{"POST", "/api/v1/apps/apply", applyBody, "incorrect", 401},
		{"POST", "/api/v1/apps/apply", strings.Replace(applyBody, "memory_mib", "memory_mb", 1), testToken, 400},
		{"POST", "/api/v1/apps/apply", applyBody + ` {}`, testToken, 400},
		{"POST", "/api/v1/apps/apply", strings.Replace(applyBody, `"deploy-1"`, `""`, 1), testToken, 400},
		{"POST", "/api/v1/apps/apply", strings.Replace(applyBody, `"memory_mib":128`, `"memory_mib":0`, 1), testToken, 400},
		{"POST", "/api/v1/apps/apply", strings.Repeat("x", agentapi.MaxRequestBytes+1), testToken, 413},
		{"POST", "/api/v1/apps/Demo/web/stop", `{"request_id":"key"}`, testToken, 400},
		{"GET", "/api/v1/jobs/0", "", testToken, 400},
		{"GET", "/api/v1/jobs/9223372036854775808", "", testToken, 400},
		{"GET", "/api/v1/operations?limit=1&limit=2", "", testToken, 400},
		{"GET", "/api/v1/operations?limit=1&bad=%zz", "", testToken, 400},
		{"DELETE", "/api/v1/apps", "", testToken, 405},
	} {
		out := request(h, test.method, test.path, test.body, test.token)
		if out.Code != test.status {
			t.Errorf("%s %s: %d != %d: %s", test.method, test.path, out.Code, test.status, out.Body.String())
		}
		var detail Error
		if json.Unmarshal(out.Body.Bytes(), &detail) != nil || detail.Code == "" {
			t.Error("non-JSON error")
		}
	}
	if f.calls != 0 {
		t.Fatal("invalid mutation reached Agent")
	}
}
func TestUncertainSubmissionIncludesOriginalKey(t *testing.T) {
	f := &fakePort{failure: &agentapi.RequestError{}}
	h := handler(t, f)
	out := request(h, "POST", "/api/v1/apps/apply", applyBody, testToken)
	var detail Error
	_ = json.Unmarshal(out.Body.Bytes(), &detail)
	if out.Code != 503 || detail.RequestID != "deploy-1" || !detail.Retryable || f.calls != 1 {
		t.Fatalf("bad uncertain result: %s", out.Body.String())
	}
	f.failure = &agentapi.HTTPError{Status: 429, Detail: agentapi.APIError{Code: "queue_full", Message: "full", Retryable: true}}
	out = request(h, "POST", "/api/v1/apps/apply", applyBody, testToken)
	if out.Code != 429 {
		t.Fatalf("Agent queue error lost: %d", out.Code)
	}
}

type blockedManager struct {
	Manager
	gate    <-chan struct{}
	entered chan<- struct{}
}

func (b blockedManager) Health(ctx context.Context) (agentapi.Health, error) {
	b.entered <- struct{}{}
	select {
	case <-b.gate:
		return agentapi.Health{APIVersion: "v1", Accepting: true}, nil
	case <-ctx.Done():
		return agentapi.Health{}, ctx.Err()
	}
}
func TestConcurrentHandlersAreBoundedWithoutUnboundedQueue(t *testing.T) {
	gate := make(chan struct{})
	defer close(gate)
	entered := make(chan struct{}, 16)
	manager := blockedManager{Manager: control.New(&fakePort{}), gate: gate, entered: entered}
	h, err := New(manager, testToken)
	if err != nil {
		t.Fatal(err)
	}
	var wait sync.WaitGroup
	for i := 0; i < 16; i++ {
		wait.Add(1)
		go func() { defer wait.Done(); request(h, "GET", "/api/v1/health", "", testToken) }()
	}
	for i := 0; i < 16; i++ {
		select {
		case <-entered:
		case <-time.After(time.Second):
			t.Fatal("handler did not start")
		}
	}
	out := request(h, "GET", "/api/v1/health", "", testToken)
	if out.Code != 503 {
		t.Fatalf("expected bounded rejection, got %d", out.Code)
	}
	// Each blocked request is released before joining.
	for i := 0; i < 16; i++ {
		gate <- struct{}{}
	}
	wait.Wait()
}
