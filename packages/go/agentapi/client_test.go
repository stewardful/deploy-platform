package agentapi

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func fakeAgent(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	root, err := os.MkdirTemp("/tmp", "dp-sdk-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	socket := filepath.Join(root, "api.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: handler, ReadHeaderTimeout: time.Second}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })
	client, err := NewClient(socket, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	return client
}
func jsonReply(w http.ResponseWriter, body string) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(body))
}
func validSubmission() Submission {
	return Submission{RequestID: "stable-key", Command: Command{Kind: "stop", App: &AppKey{Project: "demo", Name: "web"}}}
}
func TestWireContractExampleAndValidation(t *testing.T) {
	source, err := os.ReadFile("../../../contracts/agent.openapi.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract struct {
		Paths map[string]struct {
			Post struct {
				RequestBody struct {
					Content map[string]struct{ Example Submission }
				} `json:"requestBody"`
			}
		}
	}
	if err := json.Unmarshal(source, &contract); err != nil {
		t.Fatal(err)
	}
	example := contract.Paths["/v1/jobs"].Post.RequestBody.Content["application/json"].Example
	if err := example.Validate(); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"", "bad key", strings.Repeat("x", 129), "中文"} {
		request := validSubmission()
		request.RequestID = key
		if request.Validate() == nil {
			t.Fatalf("accepted invalid request ID %q", key)
		}
	}
	request := validSubmission()
	request.Command.Spec = &AppSpec{}
	if request.Validate() == nil {
		t.Fatal("accepted mixed tagged command")
	}
	id, err := NewRequestID()
	if err != nil || ValidateRequestID(id) != nil {
		t.Fatal("invalid generated ID", err)
	}
}
func TestHTTPErrorAndInvalidResponses(t *testing.T) {
	for _, test := range []struct {
		name, body, contentType string
		status                  int
		protocol                bool
	}{
		{"queue", `{"code":"queue_full","message":"full","retryable":true}`, "application/json", 429, false},
		{"bad-json", "private-malformed-content", "application/json", 200, true},
		{"bad-media", `{}`, "text/plain", 200, true},
		{"bad-version", `{"api_version":"v9","accepting":true}`, "application/json", 200, true},
		{"redirect", `{"code":"redirect","message":"no","retryable":false}`, "application/json", 302, false},
		{"oversize", strings.Repeat("x", MaxResponseBytes+1), "application/json", 200, true},
		{"unexpected-success", `{"api_version":"v1","accepting":true}`, "application/json", 202, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			client := fakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", test.contentType)
				w.Header().Set("Location", "http://example.invalid")
				w.WriteHeader(test.status)
				_, _ = w.Write([]byte(test.body))
			})
			_, err := client.Health(context.Background())
			if test.protocol {
				if !errors.Is(err, ErrProtocol) {
					t.Fatalf("expected protocol error, got %v", err)
				}
			} else {
				var upstream *HTTPError
				if !errors.As(err, &upstream) || upstream.Status != test.status {
					t.Fatalf("unexpected HTTP error %v", err)
				}
			}
		})
	}
}
func TestUncertainSubmissionIsNeverAutomaticallyRetried(t *testing.T) {
	var calls atomic.Int32
	accepted := make(chan struct{}, 1)
	client := fakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		accepted <- struct{}{}
		<-r.Context().Done()
	})
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err := client.Submit(ctx, validSubmission())
	var transport *RequestError
	if !errors.As(err, &transport) {
		t.Fatalf("expected uncertain transport error, got %v", err)
	}
	select {
	case <-accepted:
	default:
		t.Fatal("request was not submitted")
	}
	if calls.Load() != 1 {
		t.Fatal("submission retried")
	}
	if strings.Contains(err.Error(), "api.sock") {
		t.Fatal("transport error leaked socket path")
	}
}
func TestWaitCancellationDoesNotSubmitOrCancelJob(t *testing.T) {
	var calls atomic.Int32
	client := fakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/v1/jobs/7" {
			t.Error("unexpected mutation while waiting")
		}
		calls.Add(1)
		jsonReply(w, `{"id":7,"request_id":"stable-key","state":"running","result":null,"error":null}`)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	_, err := client.Wait(ctx, 7, 100*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) || calls.Load() != 1 {
		t.Fatalf("bad polling cancellation %v calls=%d", err, calls.Load())
	}
}
func TestMissingSocketDoesNotFallbackToTCP(t *testing.T) {
	client, err := NewClient(filepath.Join(t.TempDir(), "missing.sock"), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_, err = client.Health(context.Background())
	var transport *RequestError
	if !errors.As(err, &transport) {
		t.Fatal("expected transport failure", err)
	}
}
