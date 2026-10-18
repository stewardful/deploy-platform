package agentapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"path/filepath"
	"strconv"
	"time"
)

var ErrProtocol = errors.New("invalid or incompatible Agent API response")

// RequestError deliberately omits the socket path, URL and raw transport diagnostics.
// A failed POST can already have been accepted; this client never retries it.
type RequestError struct{ Cause error }

func (e *RequestError) Error() string {
	return "Agent request failed; a submitted job may already be accepted"
}
func (e *RequestError) Unwrap() error { return e.Cause }

type Client struct {
	http      *http.Client
	transport *http.Transport
}

func NewClient(socket string, timeout time.Duration) (*Client, error) {
	if !filepath.IsAbs(socket) || timeout <= 0 || timeout > 60*time.Second {
		return nil, invalid("socket or timeout")
	}
	transport := &http.Transport{
		Proxy: nil, DisableKeepAlives: true, MaxConnsPerHost: 16,
		MaxResponseHeaderBytes: 16 * 1024,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}
	return &Client{http: &http.Client{
		Transport: transport, Timeout: timeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}, transport: transport}, nil
}
func (c *Client) Close() { c.transport.CloseIdleConnections() }
func (c *Client) CreateSecret(ctx context.Context, input SecretInput) (SecretInfo, error) {
	if err := input.Validate(); err != nil {
		return SecretInfo{}, err
	}
	var value SecretInfo
	err := c.request(ctx, http.MethodPost, "/v1/secrets", input, http.StatusAccepted, &value)
	return value, err
}
func (c *Client) Secrets(ctx context.Context, project string) ([]SecretInfo, error) {
	if err := (AppKey{Project: project, Name: "check"}).Validate(); err != nil {
		return nil, err
	}
	value := []SecretInfo{}
	err := c.request(ctx, http.MethodGet, "/v1/secrets/"+project, nil, http.StatusOK, &value)
	return value, err
}
func (c *Client) Volumes(ctx context.Context, app AppKey) ([]VolumeInfo, error) {
	if err := app.Validate(); err != nil {
		return nil, err
	}
	value := []VolumeInfo{}
	err := c.request(ctx, http.MethodGet, "/v1/volumes/"+app.Project+"/"+app.Name, nil, http.StatusOK, &value)
	return value, err
}
func (c *Client) Health(ctx context.Context) (Health, error) {
	var value Health
	err := c.request(ctx, http.MethodGet, "/v1/health", nil, http.StatusOK, &value)
	if err == nil && value.APIVersion != APIVersion {
		err = ErrProtocol
	}
	return value, err
}
func (c *Client) Applications(ctx context.Context) ([]Application, error) {
	value := []Application{}
	err := c.request(ctx, http.MethodGet, "/v1/apps", nil, http.StatusOK, &value)
	if err == nil && value == nil {
		err = ErrProtocol
	}
	return value, err
}
func (c *Client) Operations(ctx context.Context, limit uint16) ([]Operation, error) {
	if limit < 1 || limit > 1000 {
		return nil, invalid("limit; expected 1..1000")
	}
	value := []Operation{}
	err := c.request(ctx, http.MethodGet, "/v1/operations?limit="+strconv.Itoa(int(limit)), nil, http.StatusOK, &value)
	if err == nil && value == nil {
		err = ErrProtocol
	}
	return value, err
}
func (c *Client) Submit(ctx context.Context, submission Submission) (Job, error) {
	if err := submission.Validate(); err != nil {
		return Job{}, err
	}
	var job Job
	err := c.request(ctx, http.MethodPost, "/v1/jobs", submission, http.StatusAccepted, &job)
	if err == nil {
		err = validateJob(job)
		if err == nil && job.RequestID != submission.RequestID {
			err = ErrProtocol
		}
	}
	return job, err
}
func (c *Client) Job(ctx context.Context, id int64) (Job, error) {
	if id < 1 {
		return Job{}, invalid("job ID")
	}
	var job Job
	err := c.request(ctx, http.MethodGet, "/v1/jobs/"+strconv.FormatInt(id, 10), nil, http.StatusOK, &job)
	if err == nil {
		err = validateJob(job)
		if err == nil && job.ID != id {
			err = ErrProtocol
		}
	}
	return job, err
}

// Wait stops polling on context cancellation; it never cancels the Agent job.
// Failed terminal jobs are returned as Job values; callers must inspect State/Error.
func (c *Client) Wait(ctx context.Context, id int64, interval time.Duration) (Job, error) {
	if interval < 100*time.Millisecond {
		return Job{}, invalid("poll interval; minimum 100ms")
	}
	for {
		job, err := c.Job(ctx, id)
		if err != nil || job.Terminal() {
			return job, err
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return Job{}, ctx.Err()
		case <-timer.C:
		}
	}
}
func validateJob(j Job) error {
	if j.ID < 1 || j.RequestID == "" {
		return ErrProtocol
	}
	switch j.State {
	case "queued", "running":
	case "succeeded":
		if len(j.Result) == 0 || bytes.Equal(bytes.TrimSpace(j.Result), []byte("null")) || j.Error != nil {
			return ErrProtocol
		}
	case "failed":
		if j.Error == nil || j.Error.Code == "" {
			return ErrProtocol
		}
	default:
		return ErrProtocol
	}
	return nil
}
func (c *Client) request(ctx context.Context, method, path string, input any, expected int, output any) error {
	var body []byte
	var err error
	if input != nil {
		body, err = json.Marshal(input)
		if err != nil || len(body) > MaxRequestBytes {
			return invalid("request encoding or size")
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://localhost"+path, bytes.NewReader(body))
	if err != nil {
		return &RequestError{Cause: err}
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return &RequestError{Cause: err}
	}
	defer resp.Body.Close()
	media, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		return ErrProtocol
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, MaxResponseBytes+1))
	if err != nil {
		return &RequestError{Cause: err}
	}
	if len(data) > MaxResponseBytes {
		return ErrProtocol
	}
	if resp.StatusCode != expected {
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return ErrProtocol
		}
		var detail APIError
		if json.Unmarshal(data, &detail) != nil || detail.Code == "" || detail.Message == "" {
			return ErrProtocol
		}
		return &HTTPError{Status: resp.StatusCode, Detail: detail}
	}
	if err := json.Unmarshal(data, output); err != nil {
		return ErrProtocol
	}
	return nil
}

func (c *Client) Diagnostics(ctx context.Context, app AppKey) (Diagnostics, error) {
	if err := app.Validate(); err != nil {
		return Diagnostics{}, err
	}
	var value Diagnostics
	err := c.request(ctx, http.MethodGet, "/v1/diagnostics/"+app.Project+"/"+app.Name, nil, http.StatusOK, &value)
	return value, err
}
func (c *Client) Logs(ctx context.Context, app AppKey, tail uint16) (LogSnapshot, error) {
	if err := app.Validate(); err != nil {
		return LogSnapshot{}, err
	}
	if tail < 1 || tail > 500 {
		return LogSnapshot{}, invalid("tail; expected 1..500")
	}
	var value LogSnapshot
	err := c.request(ctx, http.MethodGet, "/v1/logs/"+app.Project+"/"+app.Name+"?tail="+strconv.Itoa(int(tail)), nil, http.StatusOK, &value)
	return value, err
}

func (c *Client) Routes(ctx context.Context) (IngressSnapshot, error) {
	var value IngressSnapshot
	err := c.request(ctx, http.MethodGet, "/v1/ingress", nil, http.StatusOK, &value)
	return value, err
}
func (c *Client) ChangeRoute(ctx context.Context, input RouteChange) (RouteReceipt, error) {
	if err := input.Route.Validate(); err != nil {
		return RouteReceipt{}, err
	}
	if input.ExpectedRevision < 0 {
		return RouteReceipt{}, invalid("revision")
	}
	var value RouteReceipt
	err := c.request(ctx, http.MethodPost, "/v1/ingress", input, http.StatusAccepted, &value)
	return value, err
}
func (c *Client) DeleteRoute(ctx context.Context, input RouteDelete) (RouteReceipt, error) {
	if !ValidDomain(input.Domain) || input.ExpectedRevision < 1 {
		return RouteReceipt{}, invalid("domain or revision")
	}
	var value RouteReceipt
	err := c.request(ctx, http.MethodPost, "/v1/ingress/delete", input, http.StatusAccepted, &value)
	return value, err
}
