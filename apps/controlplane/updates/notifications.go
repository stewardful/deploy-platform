package updates

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"deploy-platform.local/controlplane/notifications"
)

type AutoAttempt struct {
	Schema     int    `json:"schema_version"`
	Repository string `json:"repository"`
	RunID      string `json:"run_id"`
	Phase      string `json:"phase"`
}
type NotificationStatus struct {
	Enabled        bool         `json:"enabled"`
	DevAutoUpgrade bool         `json:"dev_auto_upgrade"`
	DevTag         string       `json:"dev_tag"`
	Error          string       `json:"error"`
	LastAttempt    *AutoAttempt `json:"last_attempt"`
}

func (s *Service) lastAttempt(repo string) (*AutoAttempt, error) {
	path := filepath.Join(s.dir, "notification-dev.json")
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return nil, nil
	}
	data, err := privateRead(path, 4096)
	if err != nil {
		return nil, ErrUnavailable
	}
	var a AutoAttempt
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&a) != nil || decoder.Decode(new(any)) != io.EOF || a.Schema != 1 || !repository.MatchString(a.Repository) || strings.Contains(a.Repository, "..") || (repo != "" && a.Repository != repo) {
		return nil, ErrUnavailable
	}
	if _, err := runNumber(a.RunID); err != nil {
		return nil, ErrUnavailable
	}
	switch a.Phase {
	case "attempted", "succeeded", "failed", "skipped":
	default:
		return nil, ErrUnavailable
	}
	return &a, nil
}
func runNumber(value string) (uint64, error) {
	if value == "" || value[0] == '0' {
		return 0, ErrUnavailable
	}
	n, err := strconv.ParseUint(value, 10, 64)
	if err != nil || n == 0 {
		return 0, ErrUnavailable
	}
	return n, nil
}
func newerRun(a, b string) bool {
	x, e := runNumber(a)
	y, f := runNumber(b)
	return e == nil && (b == "" || f == nil && x > y)
}

// Inbox is a bounded, durable task hint. Only validated webhook fields enter it.
type noticeInbox struct {
	Submitted string                          `json:"submitted"`
	Schema    int                             `json:"schema_version"`
	Latest    map[string]notifications.Notice `json:"latest"`
}

func (s *Service) inbox() (noticeInbox, error) {
	value := noticeInbox{Schema: 1, Latest: map[string]notifications.Notice{}}
	path := filepath.Join(s.dir, "notification-inbox.json")
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return value, nil
	}
	data, err := privateRead(path, 4096)
	if err != nil {
		return value, err
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if d.Decode(&value) != nil || d.Decode(new(any)) != io.EOF || value.Schema != 1 || value.Latest == nil || len(value.Latest) > 2 {
		return value, ErrUnavailable
	}
	if value.Submitted != "" {
		if _, err := runNumber(value.Submitted); err != nil {
			return value, ErrUnavailable
		}
	}
	for k, n := range value.Latest {
		if k != n.Channel || !n.Valid() {
			return value, ErrUnavailable
		}
	}
	return value, nil
}
func (s *Service) acceptNotice(n notifications.Notice) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	cfg, _, err := s.loadSettings(false)
	if err != nil || n.Repository != cfg.Repository || !n.Valid() {
		return ErrUnavailable
	}
	inbox, err := s.inbox()
	if err != nil {
		return err
	}
	old, exists := inbox.Latest[n.Channel]
	if exists && (n.Channel == "dev" && !newerRun(strings.TrimPrefix(n.Tag, "dev-"), strings.TrimPrefix(old.Tag, "dev-")) || n.Channel == "stable" && !newer(strings.TrimPrefix(n.Tag, "v"), strings.TrimPrefix(old.Tag, "v"))) {
		return nil
	}
	inbox.Latest[n.Channel] = n
	if err := s.writeInbox(inbox); err != nil {
		return err
	}
	select {
	case s.wake <- struct{}{}:
	default:
	}
	return nil
}

func (s *Service) writeInbox(inbox noticeInbox) error {
	data, err := json.Marshal(inbox)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(s.dir, ".notice-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(f.Name(), filepath.Join(s.dir, "notification-inbox.json"))
	}
	if err != nil {
		return err
	}
	dir, err := os.Open(s.dir)
	if err != nil {
		return err
	}
	err = dir.Sync()
	_ = dir.Close()
	if err != nil {
		return err
	}
	return nil
}

// WebhookHandler only verifies and persists work; it never waits for an upgrade.
func (s *Service) WebhookHandler() http.Handler {
	return notifications.NewHandler(func() (notifications.Config, error) {
		cfg, _, err := s.loadSettings(false)
		if err != nil || !filepath.IsAbs(cfg.Notifications.SecretFile) {
			return notifications.Config{}, ErrUnavailable
		}
		secret, err := privateRead(cfg.Notifications.SecretFile, 1024)
		return notifications.Config{Repository: cfg.Repository, Secret: bytes.TrimSpace(secret)}, err
	}, s.acceptNotice)
}

// Run processes the local inbox. Its ticker retries busy tasks, not GitHub version polling.
func (s *Service) Run(ctx context.Context) {
	retry := time.NewTicker(5 * time.Second)
	defer retry.Stop()
	boot := time.NewTimer(10 * time.Second)
	defer boot.Stop()
	stableTag := ""
	bootCheck := false
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.wake:
		case <-retry.C:
		case <-boot.C:
			bootCheck = true
		}
		cfg, _, err := s.loadSettings(false)
		s.mu.Lock()
		s.status.Notifications.DevAutoUpgrade = err == nil && cfg.Notifications.DevAutoUpgrade
		secret, secretErr := privateRead(cfg.Notifications.SecretFile, 1024)
		s.status.Notifications.Enabled = err == nil && secretErr == nil && len(bytes.TrimSpace(secret)) >= 32
		inbox, inboxErr := s.inbox()
		if inboxErr != nil {
			s.status.Notifications.Error = "通知任务记录异常，请手动检查"
		}
		s.mu.Unlock()
		if err != nil || inboxErr != nil {
			continue
		}
		for _, n := range inbox.Latest {
			if n.Repository != cfg.Repository {
				inboxErr = ErrUnavailable
			}
		}
		if inboxErr != nil {
			continue
		}
		stableNotice, hasStable := inbox.Latest["stable"]
		s.mu.Lock()
		ready := s.last.IsZero() || s.now().Sub(s.last) >= time.Minute
		s.mu.Unlock()
		if ready && (bootCheck || hasStable && stableTag != stableNotice.Tag) {
			_, _ = s.Check(ctx)
			stableTag = stableNotice.Tag
			bootCheck = false
		}
		dev, ok := inbox.Latest["dev"]
		if !ok {
			continue
		}
		s.mu.Lock()
		s.status.Notifications.DevTag = dev.Tag
		s.mu.Unlock()
		if !cfg.Notifications.DevAutoUpgrade {
			continue
		}
		run := strings.TrimPrefix(dev.Tag, "dev-")
		attempt, err := s.lastAttempt(cfg.Repository)
		if err != nil {
			s.mu.Lock()
			s.status.Notifications.Error = "自动升级记录异常，请手动检查"
			s.mu.Unlock()
			continue
		}
		if inbox.Submitted != "" && !newerRun(run, inbox.Submitted) {
			continue
		}
		if attempt != nil && !newerRun(run, attempt.RunID) {
			continue
		}
		err = s.submitDev(cfg.Repository, run)
		if err != nil && !errors.Is(err, ErrConflict) {
			s.mu.Lock()
			s.status.Notifications.Error = "无法提交自动升级，请检查节点状态"
			s.mu.Unlock()
		}
	}
}

// submitDev re-reads durable inbox state while holding the submission-side mutex.
func (s *Service) submitDev(repo, run string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	inbox, err := s.inbox()
	if err != nil {
		return err
	}
	dev := inbox.Latest["dev"]
	if dev.Repository != repo || dev.Tag != "dev-"+run {
		return ErrConflict
	}
	if inbox.Submitted != "" && !newerRun(run, inbox.Submitted) {
		return nil
	}
	request := struct {
		Channel    string `json:"channel"`
		Repository string `json:"repository"`
		RunID      string `json:"run_id"`
		Phase      string `json:"phase"`
	}{"dev-notice", repo, run, "queued"}
	// A crash between queue and inbox commit can leave a terminal result.
	var result Execution
	data, readErr := privateRead(filepath.Join(s.dir, "result.json"), 4096)
	completed := readErr == nil && json.Unmarshal(data, &result) == nil && result.Channel == "dev-notice" && result.RunID == run && (result.Phase == "failed" || result.Phase == "succeeded" || result.Phase == "skipped")
	if !completed {
		if err := s.publishRequest(request); err != nil {
			return err
		}
	}
	inbox.Submitted = run
	return s.writeInbox(inbox)
}
