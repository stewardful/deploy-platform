package updates

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
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
	Connected      bool         `json:"connected"`
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

type notificationFrame struct {
	snapshot   notifications.Snapshot
	reconnect  bool
	generation uint64
}

// Run keeps one local SSE connection; retries reconnects/queued work, never polls versions.
func (s *Service) Run(ctx context.Context) {
	cfg, _, err := s.loadSettings(false)
	if err != nil {
		s.mu.Lock()
		s.status.Notifications.Error = "通知配置不可用"
		s.mu.Unlock()
		return
	}
	client, err := notifications.New(cfg.Notifications.Endpoint)
	if err != nil {
		s.mu.Lock()
		s.status.Notifications.Error = "通知接口必须是本机 SSE 地址"
		s.mu.Unlock()
		return
	}
	frames := make(chan notificationFrame, 1)
	disconnected := make(chan uint64, 1)
	go func() {
		delay := time.Second
		var generation uint64
		for ctx.Err() == nil {
			generation++
			first := true
			received := false
			_ = client.Stream(ctx, func(snapshot notifications.Snapshot) {
				frame := notificationFrame{snapshot: snapshot, reconnect: first, generation: generation}
				first = false
				received = true
				select {
				case previous := <-frames:
					frame.reconnect = frame.reconnect || previous.reconnect
				default:
				}
				select {
				case frames <- frame:
				case <-ctx.Done():
				}
			})
			if ctx.Err() != nil {
				return
			}
			select {
			case disconnected <- generation:
			default:
			}
			if received {
				delay = time.Second
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(delay):
			}
			if delay < 30*time.Second {
				delay *= 2
				if delay > 30*time.Second {
					delay = 30 * time.Second
				}
			}
		}
	}()
	// One boot check supplements missed webhooks. No hourly version timer remains.
	boot := time.NewTimer(10 * time.Second)
	defer boot.Stop()
	retry := time.NewTicker(5 * time.Second)
	defer retry.Stop()
	var lastDisconnected uint64
	pendingRun := ""
	reconcile := true
	stablePending := false
	previousAuto := false
	for {
		select {
		case <-ctx.Done():
			return
		case <-boot.C:
			stablePending = true
		case <-retry.C:
		case generation := <-disconnected:
			lastDisconnected = generation
			s.mu.Lock()
			s.status.Notifications.Connected = false
			s.status.Notifications.Error = "通知连接已断开，正在重连"
			s.mu.Unlock()
		case frame := <-frames:
			cfg, _, err = s.loadSettings(false)
			if err != nil {
				continue
			}
			valid := true
			for _, n := range frame.snapshot.Latest {
				if n.Repository != cfg.Repository {
					valid = false
				}
			}
			if !valid {
				s.mu.Lock()
				s.status.Notifications.Error = "通知来源与本机仓库不符"
				s.mu.Unlock()
				continue
			}
			s.mu.Lock()
			s.status.Notifications.Connected = frame.generation > lastDisconnected
			if s.status.Notifications.Connected {
				s.status.Notifications.Error = ""
			}
			if dev, ok := frame.snapshot.Latest["dev"]; ok {
				s.status.Notifications.DevTag = dev.Tag
				run := strings.TrimPrefix(dev.Tag, "dev-")
				if newerRun(run, pendingRun) {
					pendingRun = run
				}
			}
			s.mu.Unlock()
			if frame.reconnect {
				reconcile = true
				stablePending = true
			}
			if n, ok := frame.snapshot.Latest["stable"]; ok {
				s.mu.Lock()
				c := s.status.Candidate
				needs := c == nil || c.Version != strings.TrimPrefix(n.Tag, "v")
				s.mu.Unlock()
				if needs {
					stablePending = true
				}
			}
		}
		if stablePending {
			s.mu.Lock()
			ready := s.last.IsZero() || s.now().Sub(s.last) >= time.Minute
			s.mu.Unlock()
			if ready {
				_, _ = s.Check(ctx)
				stablePending = false
			} // discovery only; never Upgrade.
		}
		cfg, _, err = s.loadSettings(false)
		if err == nil && cfg.Notifications.DevAutoUpgrade && !previousAuto {
			reconcile = true
		}
		previousAuto = err == nil && cfg.Notifications.DevAutoUpgrade
		s.mu.Lock()
		s.status.Notifications.DevAutoUpgrade = err == nil && cfg.Notifications.DevAutoUpgrade
		s.mu.Unlock()
		if err != nil || notifications.ValidateEndpoint(cfg.Notifications.Endpoint) != nil || !cfg.Notifications.DevAutoUpgrade {
			continue
		}
		if pendingRun == "" && !reconcile {
			continue
		}
		attempt, err := s.lastAttempt(cfg.Repository)
		if err != nil {
			s.mu.Lock()
			s.status.Notifications.Error = "自动升级记录异常，请手动检查"
			s.mu.Unlock()
			continue
		}
		if attempt != nil {
			if pendingRun != "" && !newerRun(pendingRun, attempt.RunID) {
				pendingRun = ""
			}
			if attempt.Phase == "failed" || attempt.Phase == "attempted" {
				reconcile = false
			}
		}
		if pendingRun == "" && !reconcile {
			continue
		}
		request := struct {
			Channel    string `json:"channel"`
			Repository string `json:"repository"`
			RunID      string `json:"run_id,omitempty"`
			Phase      string `json:"phase"`
		}{"dev-notice", cfg.Repository, pendingRun, "queued"}
		s.mu.Lock()
		err = s.publishRequest(request)
		if err == nil {
			pendingRun = ""
			reconcile = false
		} else if err != ErrConflict {
			s.status.Notifications.Error = "无法提交自动升级，请检查节点状态"
		}
		s.mu.Unlock()
	}
}
