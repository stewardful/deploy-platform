package updates

import (
	"context"
	"deploy-platform.local/controlplane/notifications"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func waitFor(t *testing.T, f func() bool) {
	t.Helper()
	end := time.Now().Add(8 * time.Second)
	for time.Now().Before(end) {
		if f() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("notification deadline")
}
func notificationService(t *testing.T, auto bool, source string, attempt string, beforeStart ...func(*Service)) (*Service, func(), <-chan struct{}) {
	t.Helper()
	s := fixture(t)
	if attempt != "" {
		if err := os.WriteFile(filepath.Join(s.dir, "notification-dev.json"), []byte(attempt), 0600); err != nil {
			t.Fatal(err)
		}
	}
	n := notifications.Notice{Repository: source, Channel: "dev", Tag: "dev-42", ReleaseID: 1, PublishedAt: time.Now().UTC()}
	stable := notifications.Notice{Repository: source, Channel: "stable", Tag: "v0.5.2", ReleaseID: 2, PublishedAt: time.Now().UTC()}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		data, _ := json.Marshal(notifications.Snapshot{Revision: 1, Latest: map[string]notifications.Notice{"dev": n, "stable": stable}})
		fmt.Fprintf(w, "event: snapshot\ndata: %s\n\n", data)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	t.Cleanup(server.Close)
	config, _ := json.Marshal(Settings{Repository: "stewardful/deploy-platform", Notifications: NotificationSettings{Endpoint: server.URL + "/v1/events", DevAutoUpgrade: auto}})
	if err := os.WriteFile(s.config, config, 0600); err != nil {
		t.Fatal(err)
	}
	s.client.Transport = transport(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(release(runtime.GOARCH)))}, nil
	})
	for _, setup := range beforeStart {
		setup(s)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); s.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Error("consumer did not stop")
		}
	})
	return s, cancel, done
}
func TestStableNoticeOnlyDiscoversAndDevAutoDefaultsOff(t *testing.T) {
	s, _, _ := notificationService(t, false, "stewardful/deploy-platform", "")
	waitFor(t, func() bool { return s.Status().Candidate != nil })
	status := s.Status()
	if !status.Notifications.Connected || status.Notifications.DevAutoUpgrade || status.Notifications.DevTag != "dev-42" {
		t.Fatal(status)
	}
	if _, err := os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("notice performed an unapproved upgrade")
	}
}
func TestEnabledDevQueuesOnlyFixedHintToExistingWorker(t *testing.T) {
	s, _, _ := notificationService(t, true, "stewardful/deploy-platform", "")
	waitFor(t, func() bool { _, err := os.Stat(filepath.Join(s.dir, "request.json")); return err == nil })
	data, err := privateRead(filepath.Join(s.dir, "request.json"), 4096)
	if err != nil {
		t.Fatal(err)
	}
	var request map[string]any
	_ = json.Unmarshal(data, &request)
	if request["channel"] != "dev-notice" || request["run_id"] != "42" || len(request) != 4 {
		t.Fatalf("not a bounded hint: %s", data)
	}
	if strings.Contains(string(data), "url") || strings.Contains(string(data), "command") {
		t.Fatal("executable metadata queued")
	}
}
func TestFailedVerifiedBuildIsNotQueuedOnReconnectOrRestart(t *testing.T) {
	ledger := `{"schema_version":1,"repository":"stewardful/deploy-platform","run_id":"42","phase":"failed"}`
	s, _, _ := notificationService(t, true, "stewardful/deploy-platform", ledger)
	waitFor(t, func() bool { return s.Status().Notifications.Connected })
	if _, err := os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("failed build retried")
	}
	if s.Status().Notifications.LastAttempt.Phase != "failed" {
		t.Fatal("failure receipt lost")
	}
}
func TestWrongNotificationRepositoryAndMalformedLedgerFailClosed(t *testing.T) {
	s, _, _ := notificationService(t, false, "fork/repo", "")
	waitFor(t, func() bool { return s.Status().Notifications.Error != "" })
	if _, err := os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("untrusted source queued")
	}
	for _, body := range []string{`{}`, `{"schema_version":2}`, `{"schema_version":1,"repository":"stewardful/deploy-platform","run_id":"0","phase":"failed"}`} {
		if err := os.WriteFile(filepath.Join(s.dir, "notification-dev.json"), []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := s.lastAttempt("stewardful/deploy-platform"); err == nil {
			t.Fatal("old/invalid receipt accepted")
		}
	}
}
func TestAutomaticSubmissionSharesManualLock(t *testing.T) {
	s := fixture(t)
	if err := s.publishRequest(map[string]string{"channel": "dev-notice", "repository": "stewardful/deploy-platform", "run_id": "42"}); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.status.Candidate = &Candidate{Version: "0.5.2"}
	s.last = s.now()
	s.mu.Unlock()
	if _, err := s.Upgrade("0.5.2"); err != ErrConflict {
		t.Fatal("manual and auto overlapped", err)
	}
}

func TestPendingAutomaticNoticeWaitsForExistingTaskWithoutResubmittingIt(t *testing.T) {
	s, _, _ := notificationService(t, true, "stewardful/deploy-platform", "", func(s *Service) {
		if err := os.WriteFile(filepath.Join(s.dir, "active.json"), []byte(`{"channel":"stable","phase":"installing"}`), 0600); err != nil {
			t.Fatal(err)
		}
	})
	waitFor(t, func() bool { return s.Status().Notifications.Connected })
	if _, err := os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("auto request overlapped active task")
	}
	if err := os.Remove(filepath.Join(s.dir, "active.json")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { _, err := os.Stat(filepath.Join(s.dir, "request.json")); return err == nil })
	data, _ := privateRead(filepath.Join(s.dir, "request.json"), 4096)
	if !strings.Contains(string(data), `"run_id":"42"`) {
		t.Fatal("pending fixed build was lost")
	}
}
