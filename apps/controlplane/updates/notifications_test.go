package updates

import (
	"context"
	"deploy-platform.local/controlplane/notifications"
	"encoding/json"
	"io"
	"net/http"
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
	secret := filepath.Join(s.dir, "webhook.secret")
	if err := os.WriteFile(secret, []byte(strings.Repeat("a", 32)), 0600); err != nil {
		t.Fatal(err)
	}
	config, _ := json.Marshal(Settings{Repository: "stewardful/deploy-platform", Notifications: NotificationSettings{SecretFile: secret, DevAutoUpgrade: auto}})
	if err := os.WriteFile(s.config, config, 0600); err != nil {
		t.Fatal(err)
	}
	s.client.Transport = transport(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(release(runtime.GOARCH)))}, nil
	})
	for _, setup := range beforeStart {
		setup(s)
	}
	n := notifications.Notice{Repository: source, Channel: "dev", Tag: "dev-42", ReleaseID: 1, PublishedAt: time.Now().UTC()}
	err := s.acceptNotice(n)
	if source == "stewardful/deploy-platform" && err != nil {
		t.Fatal(err)
	}
	if source != "stewardful/deploy-platform" && err == nil {
		t.Fatal("wrong source accepted")
	}
	if source == "stewardful/deploy-platform" {
		n.Channel = "stable"
		n.Tag = "v0.5.2"
		if err := s.acceptNotice(n); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); s.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Error("worker did not stop")
		}
	})
	return s, cancel, done
}
func TestStableNoticeOnlyDiscoversAndDevAutoDefaultsOff(t *testing.T) {
	s, _, _ := notificationService(t, false, "stewardful/deploy-platform", "")
	waitFor(t, func() bool { return s.Status().Candidate != nil })
	status := s.Status()
	if !status.Notifications.Enabled || status.Notifications.DevAutoUpgrade || status.Notifications.DevTag != "dev-42" {
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
func TestFailedVerifiedBuildIsNotQueuedOnDuplicateOrRestart(t *testing.T) {
	ledger := `{"schema_version":1,"repository":"stewardful/deploy-platform","run_id":"42","phase":"failed"}`
	s, _, _ := notificationService(t, true, "stewardful/deploy-platform", ledger)
	waitFor(t, func() bool { return s.Status().Notifications.Enabled })
	if _, err := os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("failed build retried")
	}
	if s.Status().Notifications.LastAttempt.Phase != "failed" {
		t.Fatal("failure receipt lost")
	}
}
func TestWrongNotificationRepositoryAndMalformedLedgerFailClosed(t *testing.T) {
	s, _, _ := notificationService(t, false, "fork/repo", "")
	waitFor(t, func() bool { return s.Status().Notifications.Enabled })
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
	waitFor(t, func() bool { return s.Status().Notifications.Enabled })
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

func TestWebhookInboxPersistsAndOlderDuplicatesDoNotAdvance(t *testing.T) {
	s := fixture(t)
	n := notifications.Notice{Repository: "stewardful/deploy-platform", Channel: "dev", Tag: "dev-42", ReleaseID: 1, PublishedAt: time.Now().UTC()}
	if err := s.acceptNotice(n); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(filepath.Join(s.dir, "notification-inbox.json"))
	for _, tag := range []string{"dev-42", "dev-41"} {
		n.Tag = tag
		if err := s.acceptNotice(n); err != nil {
			t.Fatal(err)
		}
	}
	after, _ := os.ReadFile(filepath.Join(s.dir, "notification-inbox.json"))
	if string(before) != string(after) {
		t.Fatal("duplicate/older event changed durable inbox")
	}
	restarted := &Service{dir: s.dir}
	inbox, err := restarted.inbox()
	if err != nil || inbox.Latest["dev"].Tag != "dev-42" {
		t.Fatal("restart lost notification", err)
	}
	if err := os.Chmod(filepath.Join(s.dir, "notification-inbox.json"), 0644); err != nil {
		t.Fatal(err)
	}
	n.Tag = "dev-43"
	if err := s.acceptNotice(n); err == nil {
		t.Fatal("untrusted inbox accepted")
	}
}
func TestSubmittedTaskDoesNotRetryAfterResultIsReplaced(t *testing.T) {
	s, cancel, done := notificationService(t, true, "stewardful/deploy-platform", "")
	waitFor(t, func() bool {
		s.mu.Lock()
		defer s.mu.Unlock()
		inbox, err := s.inbox()
		return err == nil && inbox.Submitted == "42"
	})
	cancel()
	<-done
	if err := os.Remove(filepath.Join(s.dir, "request.json")); err != nil {
		t.Fatal(err)
	}
	// Another manual operation may replace result.json. The inbox must still remember submission.
	if err := os.WriteFile(filepath.Join(s.dir, "result.json"), []byte(`{"channel":"stable","phase":"succeeded"}`), 0600); err != nil {
		t.Fatal(err)
	}
	// A fresh process reads the durable marker, without relying on ticker timing.
	restarted := &Service{dir: s.dir}
	if err := restarted.submitDev("stewardful/deploy-platform", "42"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("submitted build retried after restart")
	}
}

func TestNewAcknowledgedNoticeCannotBeOverwrittenByStaleWork(t *testing.T) {
	s := fixture(t)
	n := notifications.Notice{Repository: "stewardful/deploy-platform", Channel: "dev", Tag: "dev-43", ReleaseID: 1, PublishedAt: time.Now().UTC()}
	if err := s.acceptNotice(n); err != nil {
		t.Fatal(err)
	}
	stable := n
	stable.Channel = "stable"
	stable.Tag = "v0.5.9"
	if err := s.acceptNotice(stable); err != nil {
		t.Fatal(err)
	}
	if err := s.submitDev(n.Repository, "42"); err != ErrConflict {
		t.Fatal("stale work submitted", err)
	}
	if err := s.submitDev(n.Repository, "43"); err != nil {
		t.Fatal(err)
	}
	inbox, err := s.inbox()
	if err != nil || inbox.Latest["dev"].Tag != "dev-43" || inbox.Latest["stable"].Tag != "v0.5.9" || inbox.Submitted != "43" {
		t.Fatal("lost acknowledged notice", inbox, err)
	}
}

func TestOnlyCurrentWebhookConfigurationIsAccepted(t *testing.T) {
	s := fixture(t)
	for _, config := range []string{`{"notifications":{"endpoint":"http://127.0.0.1:8090/v1/events"}}`, `{"notifications":{"secret_file":"relative.secret"}}`, `{"notifications":{"dev_auto_upgrade":"true"}}`} {
		if err := os.WriteFile(s.config, []byte(config), 0600); err != nil {
			t.Fatal(err)
		}
		if _, _, err := s.loadSettings(false); err == nil {
			t.Fatal("unsupported configuration accepted", config)
		}
	}
}
