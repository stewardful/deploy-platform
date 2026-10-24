package updates

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

type transport func(*http.Request) (*http.Response, error)

func (f transport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func fixture(t *testing.T) *Service {
	t.Helper()
	dir := t.TempDir()
	_ = os.Chmod(dir, 0700)
	manifest := filepath.Join(dir, "manifest.json")
	_ = os.WriteFile(manifest, []byte(`{"version":"0.5.1","arch":"`+runtime.GOARCH+`"}`), 0600)
	s, err := New(dir, manifest, filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	s.now = func() time.Time { return time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC) }
	return s
}
func release(arch string) string {
	return `{"tag_name":"v0.5.2","draft":false,"prerelease":false,"assets":[{"id":12,"name":"deploy-platform-0.5.2-linux-` + arch + `.run","size":42,"digest":"sha256:` + strings.Repeat("a", 64) + `"}]}`
}
func TestDiscoveryAndExplicitAtomicQueue(t *testing.T) {
	s := fixture(t)
	calls := 0
	s.client.Transport = transport(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.URL.String() != "https://api.github.com/repos/stewardful/deploy-platform/releases/latest" {
			t.Fatal(r.URL)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(release(runtime.GOARCH)))}, nil
	})
	value, err := s.Check(context.Background())
	if err != nil || value.Candidate == nil || value.Candidate.Version != "0.5.2" {
		t.Fatal(value, err)
	}
	if _, err = os.Stat(filepath.Join(s.dir, "request.json")); !os.IsNotExist(err) {
		t.Fatal("check must not queue upgrade")
	}
	_, _ = s.Check(context.Background())
	if calls != 1 {
		t.Fatal("check rate limit")
	}
	if _, err = s.Upgrade("0.5.3"); err != ErrConflict {
		t.Fatal(err)
	}
	value, err = s.Upgrade("0.5.2")
	if err != nil || value.Execution.Phase != "queued" {
		t.Fatal(value, err)
	}
	raw, err := privateRead(filepath.Join(s.dir, "request.json"), 4096)
	if err != nil {
		t.Fatal(err)
	}
	var req map[string]any
	_ = json.Unmarshal(raw, &req)
	if req["repository"] != "stewardful/deploy-platform" || req["asset_id"] != float64(12) {
		t.Fatal(req)
	}
	if _, err = s.Upgrade("0.5.2"); err != ErrConflict {
		t.Fatal("duplicate request accepted")
	}
}
func TestRejectUntrustedReleaseMetadata(t *testing.T) {
	valid := release(runtime.GOARCH)
	for _, body := range []string{
		strings.Replace(valid, `"prerelease":false`, `"prerelease":true`, 1),
		strings.Replace(valid, `"draft":false`, `"draft":true`, 1),
		strings.ReplaceAll(valid, "0.5.2", "../../other"),
		strings.Replace(valid, "sha256:", "sha1:", 1),
		strings.Replace(valid, `"size":42`, `"size":9999999999`, 1),
		release("unsupported"), strings.Repeat("x", 1024*1024+1),
	} {
		t.Run(body[:min(len(body), 60)], func(t *testing.T) {
			s := fixture(t)
			s.client.Transport = transport(func(*http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
			})
			if _, err := s.Check(context.Background()); err == nil {
				t.Fatal("accepted bad metadata")
			}
			if _, err := s.Upgrade("0.5.2"); err == nil {
				t.Fatal("queued failed discovery")
			}
		})
	}
}
func TestNumericVersionsAndStaleCheck(t *testing.T) {
	if !newer("0.10.0", "0.9.9") || newer("0.5.1", "0.5.1") || newer("0.5.0", "0.5.1") || newer("0.6.0-rc1", "0.5.1") {
		t.Fatal("version ordering")
	}
	s := fixture(t)
	s.status.Candidate = &Candidate{Version: "0.5.2"}
	s.last = s.now().Add(-2 * time.Hour)
	if _, err := s.Upgrade("0.5.2"); err != ErrConflict {
		t.Fatal(err)
	}
}
func TestPrivateCredentialsAndInterruptedWorker(t *testing.T) {
	s := fixture(t)
	token := filepath.Join(s.dir, "github.token")
	_ = os.WriteFile(token, []byte("secret"), 0600)
	cfg, _ := json.Marshal(Settings{Repository: "owner/private", TokenFile: token})
	_ = os.WriteFile(s.config, cfg, 0600)
	s.client.Transport = transport(func(r *http.Request) (*http.Response, error) {
		if r.Header.Get("Authorization") != "Bearer secret" || r.URL.Path != "/repos/owner/private/releases/latest" {
			t.Fatal("missing private credential")
		}
		return &http.Response{StatusCode: 403, Body: io.NopCloser(strings.NewReader("secret"))}, nil
	})
	value, err := s.Check(context.Background())
	if err == nil || strings.Contains(value.Error, "secret") {
		t.Fatal("unsafe error", err)
	}
	_ = os.Chmod(token, 0644)
	if _, _, err := s.settings(); err == nil {
		t.Fatal("public credentials accepted")
	}
	_ = os.WriteFile(filepath.Join(s.dir, "active.json"), []byte(`{"version":"0.5.2","phase":"queued"}`), 0600)
	if s.Status().Execution.Phase != "interrupted" {
		t.Fatal(s.Status())
	}
}

func TestConcurrentApprovalQueuesExactlyOnce(t *testing.T) {
	s := fixture(t)
	s.status.Candidate = &Candidate{Version: "0.5.2", AssetID: 12, Size: 42, Digest: "sha256:" + strings.Repeat("a", 64)}
	s.last = s.now()
	var wg sync.WaitGroup
	accepted := make(chan bool, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, err := s.Upgrade("0.5.2"); accepted <- err == nil }()
	}
	wg.Wait()
	close(accepted)
	count := 0
	for ok := range accepted {
		if ok {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("accepted %d concurrent upgrades", count)
	}
}

func TestCLIAndWebShareSubmissionLock(t *testing.T) {
	s := fixture(t)
	s.status.Candidate = &Candidate{Version: "0.5.2"}
	s.last = s.now()
	lock, err := os.OpenFile(filepath.Join(s.dir, "submission.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Upgrade("0.5.2"); err != ErrConflict {
		t.Fatalf("concurrent CLI approval accepted: %v", err)
	}
	_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	if _, err := s.Upgrade("0.5.2"); err != nil {
		t.Fatal(err)
	}
}

func TestDevBuildCanReturnToSameVersionStableRelease(t *testing.T) {
	for _, channel := range []string{"dev", "stable"} {
		s := fixture(t)
		s.current = "0.5.2"
		s.currentChannel = channel
		s.client.Transport = transport(func(*http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(release(runtime.GOARCH)))}, nil
		})
		status, err := s.Check(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if (status.Candidate != nil) != (channel == "dev") {
			t.Fatalf("wrong promotion candidate for %s: %+v", channel, status)
		}
	}
}

func TestDevProgressKeepsChannelAndRunIdentity(t *testing.T) {
	s := fixture(t)
	_ = os.WriteFile(filepath.Join(s.dir, "active.json"), []byte(`{"channel":"dev","run_id":"42","commit":"abc","phase":"queued"}`), 0600)
	_ = os.WriteFile(filepath.Join(s.dir, "result.json"), []byte(`{"channel":"dev","run_id":"42","commit":"abc","version":"0.5.3","phase":"installing"}`), 0600)
	lock, err := os.OpenFile(filepath.Join(s.dir, "worker.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	if value := s.Status().Execution; value.Phase != "installing" || value.Channel != "dev" || value.RunID != "42" {
		t.Fatalf("lost dev execution identity: %+v", value)
	}
}
