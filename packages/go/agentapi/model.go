// Package agentapi provides portable v1 Agent models and a bounded Unix Socket client.
// It has no database, container runtime or control-plane dependency.
package agentapi

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

const (
	APIVersion       = "v1"
	MaxRequestBytes  = 256 * 1024
	MaxResponseBytes = 1024 * 1024
)

type ValidationError struct{ Field string }

func (e *ValidationError) Error() string { return "invalid " + e.Field }
func invalid(field string) error         { return &ValidationError{Field: field} }

var identifierPattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`)
var requestIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
var imagePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9/._:@-]*$`)
var digestPattern = regexp.MustCompile(`^sha256:[0-9a-fA-F]{64}$`)
var mountComponentPattern = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
var envPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

type AppKey struct {
	Project string `json:"project"`
	Name    string `json:"name"`
}

func (a AppKey) Validate() error {
	for _, s := range []string{a.Project, a.Name} {
		if len(s) > 48 || !identifierPattern.MatchString(s) {
			return invalid("application identifier")
		}
	}
	return nil
}

type Resources struct {
	MemoryMiB uint32 `json:"memory_mib"`
	CPUMillis uint32 `json:"cpu_millis"`
	PIDs      uint32 `json:"pids"`
}
type LocalPort struct {
	Host      uint16 `json:"host"`
	Container uint16 `json:"container"`
}
type RunAs struct {
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
}
type VolumeMount struct {
	Name     string `json:"name"`
	Target   string `json:"target"`
	ReadOnly bool   `json:"read_only"`
}
type SecretMount struct {
	Name string `json:"name"`
}
type SecretInfo struct {
	Project string `json:"project"`
	Name    string `json:"name"`
}
type SecretInput struct {
	Project string `json:"project"`
	Name    string `json:"name"`
	Value   string `json:"value"`
}

func (s SecretInput) String() string   { return "SecretInput{redacted}" }
func (s SecretInput) GoString() string { return s.String() }
func (s SecretInput) Validate() error {
	if (AppKey{Project: s.Project, Name: s.Name}).Validate() != nil || len(s.Value) == 0 || len(s.Value) > 65536 || !utf8.ValidString(s.Value) || strings.ContainsRune(s.Value, '\x00') {
		return invalid("secret")
	}
	return nil
}

type VolumeInfo struct {
	Name   string `json:"name"`
	Target string `json:"target"`
	UID    uint32 `json:"uid"`
	GID    uint32 `json:"gid"`
}

func ValidVolumeTarget(s string) bool {
	if len(s) > 256 || strings.HasSuffix(s, "/") || !strings.HasPrefix(s, "/") {
		return false
	}
	valid := false
	for _, p := range []string{"/data", "/var/lib", "/srv", "/opt", "/app", "/workspace"} {
		if s == p || strings.HasPrefix(s, p+"/") {
			valid = true
		}
	}
	for _, p := range strings.Split(s[1:], "/") {
		if p == "" || p == "." || p == ".." || !mountComponentPattern.MatchString(p) {
			return false
		}
	}
	return valid
}

type HttpHealthCheck struct {
	Port uint16 `json:"port"`
	Path string `json:"path"`
}

type AppSpec struct {
	PullPolicy    string            `json:"pull_policy,omitempty"`
	HealthCheck   *HttpHealthCheck  `json:"health_check,omitempty"`
	SchemaVersion uint32            `json:"schema_version"`
	App           AppKey            `json:"app"`
	Image         string            `json:"image"`
	Resources     Resources         `json:"resources"`
	Environment   map[string]string `json:"environment,omitempty"`
	Args          []string          `json:"args,omitempty"`
	Network       string            `json:"network,omitempty"`
	Ports         []LocalPort       `json:"ports,omitempty"`
	User          *RunAs            `json:"user,omitempty"`
	Volumes       []VolumeMount     `json:"volumes,omitempty"`
	Secrets       []SecretMount     `json:"secrets,omitempty"`
}

func (s AppSpec) Validate() error {
	if s.PullPolicy != "" && s.PullPolicy != "always" && s.PullPolicy != "if_not_present" && s.PullPolicy != "never" {
		return invalid("pull policy")
	}
	if h := s.HealthCheck; h != nil {
		if s.Network != "project" || h.Port == 0 || len(h.Path) > 256 || !strings.HasPrefix(h.Path, "/") || strings.HasPrefix(h.Path, "//") || !regexp.MustCompile(`^/[A-Za-z0-9/._~-]*$`).MatchString(h.Path) {
			return invalid("HTTP health check; requires project network")
		}
	}
	if len(s.Volumes) > 8 || len(s.Secrets) > 16 {
		return invalid("mount count")
	}
	names := map[string]bool{}
	for i, v := range s.Volumes {
		if (AppKey{Project: s.App.Project, Name: v.Name}).Validate() != nil || names[v.Name] || !ValidVolumeTarget(v.Target) {
			return invalid("volume mount")
		}
		names[v.Name] = true
		for _, p := range s.Volumes[:i] {
			if v.Target == p.Target || strings.HasPrefix(v.Target, p.Target+"/") || strings.HasPrefix(p.Target, v.Target+"/") {
				return invalid("overlapping volume mounts")
			}
		}
	}
	names = map[string]bool{}
	for _, v := range s.Secrets {
		if (AppKey{Project: s.App.Project, Name: v.Name}).Validate() != nil || names[v.Name] {
			return invalid("secret reference")
		}
		names[v.Name] = true
	}

	if s.SchemaVersion != 1 {
		return invalid("schema_version; expected 1")
	}
	if err := s.App.Validate(); err != nil {
		return err
	}
	if len(s.Image) > 512 || !imagePattern.MatchString(s.Image) {
		return invalid("image reference")
	}
	if _, digest, ok := strings.Cut(s.Image, "@"); ok && !digestPattern.MatchString(digest) {
		return invalid("image digest")
	}
	if s.Resources.MemoryMiB < 16 || s.Resources.CPUMillis == 0 || s.Resources.PIDs == 0 {
		return invalid("resources")
	}
	if len(s.Args) > 128 {
		return invalid("arguments")
	}
	for _, a := range s.Args {
		if !validValue(a) {
			return invalid("arguments")
		}
	}
	if len(s.Environment) > 128 {
		return invalid("environment")
	}
	for k, v := range s.Environment {
		if len(k) > 128 || !envPattern.MatchString(k) || !validValue(v) {
			return invalid("environment")
		}
	}
	if s.Network != "" && s.Network != "none" && s.Network != "bridge" && s.Network != "project" {
		return invalid("network")
	}
	if len(s.Ports) > 16 || (len(s.Ports) > 0 && s.Network != "bridge" && s.Network != "project") {
		return invalid("ports require bridge or project; maximum 16")
	}
	used := make(map[uint16]bool)
	for _, p := range s.Ports {
		if p.Host < 1024 || p.Container == 0 || used[p.Host] {
			return invalid("ports")
		}
		used[p.Host] = true
	}
	return nil
}
func validValue(s string) bool {
	return len(s) <= 8192 && !strings.ContainsRune(s, '\x00') && utf8.ValidString(s)
}

// Command is a tagged union. Validate rejects fields belonging to another kind.
type Command struct {
	Kind          string   `json:"kind"`
	Spec          *AppSpec `json:"spec,omitempty"`
	StartupWaitMS *uint64  `json:"startup_wait_ms,omitempty"`
	App           *AppKey  `json:"app,omitempty"`
}
type Submission struct {
	RequestID string  `json:"request_id"`
	Command   Command `json:"command"`
}

func (s Submission) Validate() error {
	if err := ValidateRequestID(s.RequestID); err != nil {
		return err
	}
	c := s.Command
	switch c.Kind {
	case "apply":
		if c.Spec == nil || c.StartupWaitMS == nil || c.App != nil {
			return invalid("apply command")
		}
		if *c.StartupWaitMS < 100 || *c.StartupWaitMS > 30000 {
			return invalid("startup_wait_ms; expected 100..30000")
		}
		return c.Spec.Validate()
	case "start", "stop":
		if c.App == nil || c.Spec != nil || c.StartupWaitMS != nil {
			return invalid("intent command")
		}
		return c.App.Validate()
	default:
		return invalid("command kind")
	}
}

// NewRequestID creates a uniqueness key, not a credential. Persist it before submitting
// and reuse the same key and command after an uncertain response.
func NewRequestID() (string, error) {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", fmt.Errorf("generate request ID: %w", err)
	}
	return hex.EncodeToString(value[:]), nil
}

type Health struct {
	APIVersion     string `json:"api_version"`
	ServiceVersion string `json:"service_version"`
	Accepting      bool   `json:"accepting"`
}
type APIError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

func (e *APIError) Error() string { return e.Message }

type HTTPError struct {
	Status int
	Detail APIError
}

func (e *HTTPError) Error() string { return fmt.Sprintf("agent HTTP %d: %s", e.Status, e.Detail.Code) }

type Recovery struct {
	Attempts       uint32  `json:"attempts"`
	NextAttemptMS  uint64  `json:"next_attempt_ms"`
	RunningSinceMS uint64  `json:"running_since_ms"`
	ErrorCode      *string `json:"error_code"`
}
type Application struct {
	App         AppKey   `json:"app"`
	Fingerprint string   `json:"fingerprint"`
	Phase       string   `json:"phase"`
	InstanceID  *string  `json:"instance_id"`
	Desired     string   `json:"desired"`
	Recovery    Recovery `json:"recovery"`
}
type Operation struct {
	ID        int64   `json:"id"`
	App       AppKey  `json:"app"`
	Phase     string  `json:"phase"`
	ErrorCode *string `json:"error_code"`
}
type Job struct {
	ID        int64  `json:"id"`
	RequestID string `json:"request_id"`
	State     string `json:"state"`
	// Raw JSON preserves both tagged result variants and future additive fields.
	Result json.RawMessage `json:"result"`
	Error  *APIError       `json:"error"`
}

func (j Job) Terminal() bool { return j.State == "succeeded" || j.State == "failed" }

type AppliedResult struct {
	Kind        string `json:"kind"`
	OperationID int64  `json:"operation_id"`
	Created     bool   `json:"created"`
	InstanceID  string `json:"instance_id"`
	State       string `json:"state"`
}
type IntentResult struct {
	Kind          string `json:"kind"`
	App           AppKey `json:"app"`
	Desired       string `json:"desired"`
	Outcome       string `json:"outcome"`
	NextAttemptMS uint64 `json:"next_attempt_ms"`
}

// ValidateRequestID validates a stable mutation key without echoing its contents.
func ValidateRequestID(key string) error {
	if len(key) > 128 || !requestIDPattern.MatchString(key) {
		return invalid("request_id")
	}
	return nil
}

type Diagnostics struct {
	State       string  `json:"state"`
	Network     *string `json:"network"`
	ServiceName *string `json:"service_name"`
	Health      string  `json:"health"`
	HTTPStatus  *uint16 `json:"http_status"`
	CheckedAtMS uint64  `json:"checked_at_ms"`
}
type LogSnapshot struct {
	Text string `json:"text"`
	Tail uint16 `json:"tail"`
}

// Domain bindings contain only an AppKey and container port, never arbitrary URLs.
type DomainRoute struct {
	Domain  string `json:"domain"`
	App     AppKey `json:"app"`
	Port    uint16 `json:"port"`
	TLS     string `json:"tls"`
	Enabled bool   `json:"enabled"`
}
type RouteChange struct {
	Route            DomainRoute `json:"route"`
	ExpectedRevision int64       `json:"expected_revision"`
}
type RouteDelete struct {
	Domain           string `json:"domain"`
	ExpectedRevision int64  `json:"expected_revision"`
}
type RouteReceipt struct {
	Revision int64 `json:"revision"`
}
type RouteStatus struct {
	Route         DomainRoute `json:"route"`
	Revision      int64       `json:"revision"`
	ProxyState    string      `json:"proxy_state"`
	UpstreamReady bool        `json:"upstream_ready"`
	CheckedAtMS   uint64      `json:"checked_at_ms"`
	ErrorCode     *string     `json:"error_code"`
}
type IngressSnapshot struct {
	Routes          []RouteStatus `json:"routes"`
	Revision        int64         `json:"revision"`
	AppliedRevision int64         `json:"applied_revision"`
	CheckedAtMS     uint64        `json:"checked_at_ms"`
	ErrorCode       *string       `json:"error_code"`
}
type CertificateStatus struct {
	Domain      string  `json:"domain"`
	State       string  `json:"state"`
	Trusted     bool    `json:"trusted"`
	NotAfter    *string `json:"not_after"`
	Issuer      *string `json:"issuer"`
	CheckedAtMS int64   `json:"checked_at_ms"`
}

func ValidDomain(domain string) bool {
	if len(domain) > 253 || domain != strings.ToLower(domain) {
		return false
	}
	labels := strings.Split(domain, ".")
	if len(labels) < 2 {
		return false
	}
	for _, s := range labels {
		if len(s) > 63 || !identifierPattern.MatchString(s) {
			return false
		}
	}
	last := labels[len(labels)-1]
	if len(last) < 2 {
		return false
	}
	for _, s := range []string{"localhost", "local", "internal", "invalid", "example", "test"} {
		if last == s {
			return false
		}
	}
	if strings.HasPrefix(last, "xn--") {
		return true
	}
	for _, c := range last {
		if c < 'a' || c > 'z' {
			return false
		}
	}
	return true
}
func (r DomainRoute) Validate() error {
	if !ValidDomain(r.Domain) || r.Port == 0 || (r.TLS != "production" && r.TLS != "staging") {
		return invalid("domain, container port or TLS mode")
	}
	return r.App.Validate()
}

// Maintenance describes admission and accepted-work draining on one node.
type Maintenance struct {
	Draining      bool   `json:"draining"`
	PendingJobs   uint32 `json:"pending_jobs"`
	ActiveWriters uint32 `json:"active_writers"`
	Quiescent     bool   `json:"quiescent"`
}
