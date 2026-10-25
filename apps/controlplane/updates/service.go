// Package updates discovers trusted GitHub releases and queues explicit upgrades.
// The privileged installer runs in a separate systemd unit, surviving controld restarts.
package updates

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var stable = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)
var repository = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
var digest = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var ErrUnavailable = errors.New("update unavailable; check repository credentials and release assets")
var ErrConflict = errors.New("upgrade already pending, candidate changed, or fresh version check required")

type Candidate struct {
	Version string `json:"version"`
	AssetID int64  `json:"asset_id"`
	Size    int64  `json:"size"`
	Digest  string `json:"digest"`
}
type Execution struct {
	Channel string `json:"channel,omitempty"`
	RunID   string `json:"run_id,omitempty"`
	Commit  string `json:"commit,omitempty"`
	Version string `json:"version"`
	Phase   string `json:"phase"`
	Message string `json:"message"`
}
type Status struct {
	Enabled   bool       `json:"enabled"`
	Current   string     `json:"current"`
	Candidate *Candidate `json:"candidate"`
	CheckedAt string     `json:"checked_at"`
	Error     string     `json:"error"`
	Execution *Execution `json:"execution"`
}
type Settings struct {
	DevAutoUpgrade bool   `json:"dev_auto_upgrade"`
	Repository     string `json:"repository"`
	TokenFile      string `json:"token_file"`
}
type Service struct {
	mu                                         sync.Mutex
	dir, current, arch, config, currentChannel string
	client                                     *http.Client
	now                                        func() time.Time
	last                                       time.Time
	status                                     Status
}

func New(dir, manifest, config string) (*Service, error) {
	data, err := os.ReadFile(manifest)
	if err != nil {
		return nil, err
	}
	var release struct {
		Version string `json:"version"`
		Arch    string `json:"arch"`
		Build   struct {
			Channel string `json:"channel"`
		} `json:"build"`
	}
	if json.Unmarshal(data, &release) != nil || !stable.MatchString(release.Version) || release.Arch != runtime.GOARCH {
		return nil, errors.New("invalid installed release manifest")
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("updates directory must be a private directory")
	}
	return &Service{dir: dir, current: release.Version, currentChannel: release.Build.Channel, arch: release.Arch, config: config, now: time.Now, client: &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, status: Status{Enabled: true, Current: release.Version}}, nil
}

func privateRead(path string, limit int64) ([]byte, error) {
	before, err := os.Lstat(path)
	if err != nil || !before.Mode().IsRegular() || before.Mode().Perm()&0077 != 0 {
		return nil, ErrUnavailable
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, ErrUnavailable
	}
	defer f.Close()
	after, err := f.Stat()
	if err != nil || !os.SameFile(before, after) {
		return nil, ErrUnavailable
	}
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(b)) > limit {
		return nil, ErrUnavailable
	}
	return b, nil
}
func (s *Service) settings() (Settings, string, error) { return s.loadSettings(true) }
func (s *Service) loadSettings(readToken bool) (Settings, string, error) {
	cfg := Settings{Repository: "stewardful/deploy-platform"}
	if _, err := os.Lstat(s.config); !os.IsNotExist(err) {
		data, err := privateRead(s.config, 4096)
		decoder := json.NewDecoder(strings.NewReader(string(data)))
		decoder.DisallowUnknownFields()
		if err != nil || decoder.Decode(&cfg) != nil || decoder.Decode(new(any)) != io.EOF {
			return cfg, "", ErrUnavailable
		}
	}
	if !repository.MatchString(cfg.Repository) || strings.Contains(cfg.Repository, "..") {
		return cfg, "", ErrUnavailable
	}
	token := ""
	if cfg.TokenFile != "" && readToken {
		if !filepath.IsAbs(cfg.TokenFile) {
			return cfg, "", ErrUnavailable
		}
		data, err := privateRead(cfg.TokenFile, 1024)
		if err != nil {
			return cfg, "", err
		}
		token = strings.TrimSpace(string(data))
		if token == "" || strings.ContainsAny(token, "\r\n") {
			return cfg, "", ErrUnavailable
		}
	}
	return cfg, token, nil
}
func newer(a, b string) bool {
	if !stable.MatchString(a) || !stable.MatchString(b) {
		return false
	}
	aa, bb := strings.Split(a, "."), strings.Split(b, ".")
	for i := range aa {
		x, e := strconv.ParseUint(aa[i], 10, 32)
		y, f := strconv.ParseUint(bb[i], 10, 32)
		if e != nil || f != nil {
			return false
		}
		if x != y {
			return x > y
		}
	}
	return false
}
func (s *Service) snapshot() Status {
	value := s.status
	for _, name := range []string{"active.json", "request.json", "result.json"} {
		data, err := privateRead(filepath.Join(s.dir, name), 4096)
		if err != nil {
			continue
		}
		var e Execution
		if json.Unmarshal(data, &e) != nil {
			continue
		}
		if name == "request.json" {
			e.Phase = "queued"
			e.Message = "升级请求已排队"
		}
		if name == "active.json" {
			result, err := privateRead(filepath.Join(s.dir, "result.json"), 4096)
			var progress Execution
			if err == nil && json.Unmarshal(result, &progress) == nil && (progress.Version == e.Version || e.Channel == "dev") && progress.Channel == e.Channel && progress.RunID == e.RunID {
				e = progress
			}
			lock, err := os.OpenFile(filepath.Join(s.dir, "worker.lock"), os.O_RDWR, 0600)
			if err != nil {
				e.Phase = "interrupted"
			} else {
				if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) == nil {
					e.Phase = "interrupted"
					_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
				}
				_ = lock.Close()
			}
			if e.Phase == "interrupted" {
				e.Message = "升级执行器已中断，请查看服务日志并恢复安装事务"
			}
		}
		value.Execution = &e
		break
	}
	return value
}
func (s *Service) Status() Status { s.mu.Lock(); defer s.mu.Unlock(); return s.snapshot() }
func (s *Service) Check(ctx context.Context) (Status, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Bound authenticated manual refreshes and startup checks, including failures.
	if !s.last.IsZero() && s.now().Sub(s.last) < time.Minute {
		return s.snapshot(), nil
	}
	s.last = s.now()
	s.status.CheckedAt = s.last.UTC().Format(time.RFC3339)
	s.status.Candidate = nil
	s.status.Error = ""
	candidate, err := s.discover(ctx)
	if err != nil {
		s.status.Error = ErrUnavailable.Error()
		return s.snapshot(), ErrUnavailable
	}
	s.status.Candidate = candidate
	return s.snapshot(), nil
}
func (s *Service) discover(ctx context.Context) (*Candidate, error) {
	cfg, token, err := s.settings()
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "GET", "https://api.github.com/repos/"+cfg.Repository+"/releases/latest", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "deploy-platform-updater")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := s.client.Do(req)
	if err != nil {
		return nil, ErrUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return nil, ErrUnavailable
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+1))
	if err != nil || len(data) > 1024*1024 {
		return nil, ErrUnavailable
	}
	var release struct {
		Tag        string `json:"tag_name"`
		Draft      bool   `json:"draft"`
		Prerelease bool   `json:"prerelease"`
		Assets     []struct {
			ID     int64  `json:"id"`
			Name   string `json:"name"`
			Size   int64  `json:"size"`
			Digest string `json:"digest"`
		} `json:"assets"`
	}
	if json.Unmarshal(data, &release) != nil || release.Draft || release.Prerelease || !strings.HasPrefix(release.Tag, "v") {
		return nil, ErrUnavailable
	}
	version := strings.TrimPrefix(release.Tag, "v")
	if !stable.MatchString(version) {
		return nil, ErrUnavailable
	}
	if !newer(version, s.current) && !(version == s.current && s.currentChannel == "dev") {
		return nil, nil
	}
	name := fmt.Sprintf("deploy-platform-%s-linux-%s.run", version, s.arch)
	var result *Candidate
	for _, asset := range release.Assets {
		if asset.Name == name {
			if result != nil || asset.ID <= 0 || asset.Size <= 0 || asset.Size > 512*1024*1024 || !digest.MatchString(asset.Digest) {
				return nil, ErrUnavailable
			}
			result = &Candidate{version, asset.ID, asset.Size, asset.Digest}
		}
	}
	if result == nil {
		return nil, ErrUnavailable
	}
	return result, nil
}
func (s *Service) Upgrade(version string) (Status, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.status.Candidate
	if c == nil || version != c.Version || s.now().Sub(s.last) > time.Hour || s.status.Error != "" {
		return s.snapshot(), ErrConflict
	}
	cfg, _, err := s.settings()
	if err != nil {
		return s.snapshot(), err
	}
	// Atomic publish: systemd.path sees only a complete, durable request.
	request := struct {
		Candidate
		Repository string `json:"repository"`
		Phase      string `json:"phase"`
	}{*c, cfg.Repository, "queued"}
	err = s.publishRequest(request)
	return s.snapshot(), err
}

// publishRequest is the one Go submission boundary for manual Web requests.
// Python CLI shares its submission.lock and the same independent worker/installer.
func (s *Service) publishRequest(request any) error {
	lock, err := os.OpenFile(filepath.Join(s.dir, "submission.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return ErrConflict
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	for _, name := range []string{"request.json", "active.json"} {
		if _, err := os.Lstat(filepath.Join(s.dir, name)); !os.IsNotExist(err) {
			return ErrConflict
		}
	}
	if err := os.Remove(filepath.Join(s.dir, "result.json")); err != nil && !os.IsNotExist(err) {
		return err
	}
	data, _ := json.Marshal(request)
	f, err := os.CreateTemp(s.dir, ".request-")
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
		err = os.Rename(f.Name(), filepath.Join(s.dir, "request.json"))
	}
	if err != nil {
		return err
	}
	d, err := os.Open(s.dir)
	if err == nil {
		err = d.Sync()
		_ = d.Close()
	}
	if err != nil {
		return err
	}
	return nil
}

// Run supplements the manual stable discovery once at startup. No webhook or version polling.
func (s *Service) Run(ctx context.Context) {
	timer := time.NewTimer(10 * time.Second)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return
	case <-timer.C:
		_, _ = s.Check(ctx)
	}
}
