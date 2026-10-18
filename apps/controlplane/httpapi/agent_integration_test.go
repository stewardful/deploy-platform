package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"

	"deploy-platform.local/agentapi"
	"deploy-platform.local/controlplane/control"
)

// This opt-in test uses actual Rust routing, SQLite, worker and Go HTTP/SDK together.
// The fixture alone substitutes an in-memory runtime; no real containers are touched.
func TestRealRustAgentIntegration(t *testing.T) {
	binary := os.Getenv("AGENT_API_FIXTURE")
	if binary == "" {
		t.Skip("set AGENT_API_FIXTURE to the built Rust api_fixture binary")
	}
	if !filepath.IsAbs(binary) {
		t.Fatal("fixture path must be absolute")
	}
	root, err := os.MkdirTemp("/tmp", "dp-cross-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	state, socket := filepath.Join(root, "state"), filepath.Join(root, "run/api.sock")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	process := exec.Command(binary, state, socket)
	var output bytes.Buffer
	process.Stdout = &output
	process.Stderr = &output
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = process.Process.Signal(syscall.SIGTERM)
		done := make(chan error, 1)
		go func() { done <- process.Wait() }()
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("fixture exited: %v: %s", err, output.String())
			}
		case <-time.After(5 * time.Second):
			_ = process.Process.Kill()
			<-done
			t.Error("fixture did not stop")
		}
	})
	client, err := agentapi.NewClient(socket, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := client.Health(ctx); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("Rust API did not become ready")
		}
		time.Sleep(20 * time.Millisecond)
	}
	h, err := New(control.New(client), testToken)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(h)
	defer server.Close()
	call := func(method, path, body string, status int, target any) {
		t.Helper()
		req, err := http.NewRequestWithContext(ctx, method, server.URL+path, bytes.NewBufferString(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+testToken)
		req.Header.Set("Content-Type", "application/json")
		resp, err := server.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != status {
			t.Fatalf("%s %s: %d %s", method, path, resp.StatusCode, data)
		}
		if target != nil {
			if err := json.Unmarshal(data, target); err != nil {
				t.Fatal(err)
			}
		}
	}
	wait := func(job agentapi.Job) agentapi.Job {
		t.Helper()
		for !job.Terminal() {
			select {
			case <-ctx.Done():
				t.Fatal("job did not finish")
			case <-time.After(30 * time.Millisecond):
			}
			call("GET", "/api/v1/jobs/"+strconv.FormatInt(job.ID, 10), "", 200, &job)
		}
		if job.State != "succeeded" {
			t.Fatalf("job failed: %+v", job)
		}
		return job
	}
	var job, duplicate agentapi.Job
	call("POST", "/api/v1/apps/apply", applyBody, 202, &job)
	job = wait(job)
	var result agentapi.AppliedResult
	if err := json.Unmarshal(job.Result, &result); err != nil || !result.Created {
		t.Fatal("Go apply did not create a Rust runtime instance", err)
	}
	call("POST", "/api/v1/apps/apply", applyBody, 202, &duplicate)
	if duplicate.ID != job.ID {
		t.Fatal("stable request ID did not deduplicate across Go/Rust")
	}
	var apps []agentapi.Application
	call("GET", "/api/v1/apps", "", 200, &apps)
	if len(apps) != 1 || apps[0].App.Name != "web" || apps[0].Desired != "running" {
		t.Fatalf("unexpected apps %+v", apps)
	}
	var operations []agentapi.Operation
	call("GET", "/api/v1/operations?limit=10", "", 200, &operations)
	if len(operations) != 1 {
		t.Fatal("duplicate request added apply operation")
	}
	for _, action := range []string{"stop", "start"} {
		call("POST", "/api/v1/apps/demo/web/"+action, `{"request_id":"`+action+`-1"}`, 202, &job)
		wait(job)
		call("GET", "/api/v1/apps", "", 200, &apps)
		expected := "running"
		if action == "stop" {
			expected = "stopped"
		}
		if apps[0].Desired != expected {
			t.Fatal("intent did not persist in Rust SQLite")
		}
	}
	call("GET", "/api/v1/jobs/999999", "", 404, nil)
	call("GET", "/api/v1/operations?limit=10", "", 200, &operations)
	var secret agentapi.SecretInfo
	call("POST", "/api/v1/secrets", `{"project":"demo","name":"password","value":"private-db-password"}`, 202, &secret)
	call("POST", "/api/v1/secrets", `{"project":"demo","name":"password","value":"private-db-password"}`, 202, nil)
	call("POST", "/api/v1/secrets", `{"project":"demo","name":"password","value":"different"}`, 409, nil)
	var names []agentapi.SecretInfo
	call("GET", "/api/v1/secrets/demo", "", 200, &names)
	if len(names) != 1 || names[0].Name != "password" {
		t.Fatal("secret metadata missing")
	}
	call("POST", "/api/v1/apps/apply", `{"request_id":"missing-secret","spec":{"schema_version":1,"app":{"project":"demo","name":"db"},"image":"postgres:17-bookworm","resources":{"memory_mib":384,"cpu_millis":500,"pids":128},"secrets":[{"name":"absent"}]}}`, 404, nil)
	// Write-only values must not leak into durable queue/configuration, including WAL.
	for _, name := range []string{"agent.sqlite", "agent.sqlite-wal"} {
		data, err := os.ReadFile(filepath.Join(state, name))
		if err == nil && bytes.Contains(data, []byte("private-db-password")) {
			t.Fatal("secret value entered SQLite")
		}
	}

}
