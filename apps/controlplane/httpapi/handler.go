// Package httpapi adapts a control service to an authenticated, bounded HTTP API.
package httpapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"unicode/utf8"

	"deploy-platform.local/agentapi"
	"deploy-platform.local/controlplane/updates"
)

type Manager interface {
	Health(context.Context) (agentapi.Health, error)
	Applications(context.Context) ([]agentapi.Application, error)
	Operations(context.Context, uint16) ([]agentapi.Operation, error)
	Apply(context.Context, string, agentapi.AppSpec, uint64) (agentapi.Job, error)
	Intent(context.Context, string, agentapi.AppKey, string) (agentapi.Job, error)
	Job(context.Context, int64) (agentapi.Job, error)
}
type DiagnosticsManager interface {
	Diagnostics(context.Context, agentapi.AppKey) (agentapi.Diagnostics, error)
	Logs(context.Context, agentapi.AppKey, uint16) (agentapi.LogSnapshot, error)
}
type IngressManager interface {
	Routes(context.Context) (agentapi.IngressSnapshot, error)
	ChangeRoute(context.Context, agentapi.RouteChange) (agentapi.RouteReceipt, error)
	DeleteRoute(context.Context, agentapi.RouteDelete) (agentapi.RouteReceipt, error)
	Certificate(context.Context, string) (agentapi.CertificateStatus, error)
}
type ResourceManager interface {
	CreateSecret(context.Context, agentapi.SecretInput) (agentapi.SecretInfo, error)
	Secrets(context.Context, string) ([]agentapi.SecretInfo, error)
	Volumes(context.Context, agentapi.AppKey) ([]agentapi.VolumeInfo, error)
}
type Error struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
	RequestID string `json:"request_id,omitempty"`
}
type ApplyRequest struct {
	RequestID     string           `json:"request_id"`
	Spec          agentapi.AppSpec `json:"spec"`
	StartupWaitMS *uint64          `json:"startup_wait_ms,omitempty"`
}
type IntentRequest struct {
	RequestID string `json:"request_id"`
}

type Updater interface {
	Status() updates.Status
	Check(context.Context) (updates.Status, error)
	Upgrade(string) (updates.Status, error)
}

func New(manager Manager, token string) (http.Handler, error) {
	return NewWithUpdates(manager, token, nil)
}
func NewWithUpdates(manager Manager, token string, updater Updater) (http.Handler, error) {
	if manager == nil || len(token) < 32 || len(token) > 128 {
		return nil, errors.New("manager and 32..128 byte token required")
	}
	expected := sha256.Sum256([]byte(token))
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/updates", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		if updater == nil {
			write(w, 200, updates.Status{Enabled: false})
			return
		}
		write(w, 200, updater.Status())
	})
	mux.HandleFunc("/api/v1/updates/check", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodPost) {
			return
		}
		var input struct{}
		if !decode(w, r, &input) {
			return
		}
		if updater == nil {
			write(w, 503, Error{Code: "unavailable", Message: "updates disabled"})
			return
		}
		value, _ := updater.Check(r.Context())
		write(w, 202, value)
	})
	mux.HandleFunc("/api/v1/updates/upgrade", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodPost) {
			return
		}
		var input struct {
			Version string `json:"version"`
		}
		if !decode(w, r, &input) {
			return
		}
		if updater == nil {
			write(w, 503, Error{Code: "unavailable", Message: "updates disabled"})
			return
		}
		value, err := updater.Upgrade(input.Version)
		if err != nil {
			write(w, 409, Error{Code: "conflict", Message: "upgrade pending or version check required"})
			return
		}
		write(w, 202, value)
	})
	mux.HandleFunc("/api/v1/ingress", func(w http.ResponseWriter, r *http.Request) {
		p, ok := manager.(IngressManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		if r.Method == http.MethodGet {
			v, err := p.Routes(r.Context())
			respond(w, v, err, http.StatusOK, "")
			return
		}
		if !method(w, r, http.MethodPost) {
			return
		}
		input := agentapi.RouteChange{Route: agentapi.DomainRoute{Enabled: true, TLS: "production"}}
		if !decode(w, r, &input) {
			return
		}
		v, err := p.ChangeRoute(r.Context(), input)
		respond(w, v, err, http.StatusAccepted, "")
	})
	mux.HandleFunc("/api/v1/ingress/delete", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodPost) {
			return
		}
		p, ok := manager.(IngressManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		var input agentapi.RouteDelete
		if !decode(w, r, &input) {
			return
		}
		v, err := p.DeleteRoute(r.Context(), input)
		respond(w, v, err, http.StatusAccepted, "")
	})
	mux.HandleFunc("/api/v1/ingress/certificate/{domain}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		p, ok := manager.(IngressManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		v, err := p.Certificate(r.Context(), r.PathValue("domain"))
		respond(w, v, err, http.StatusOK, "")
	})

	mux.HandleFunc("/api/v1/diagnostics/{project}/{name}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		p, ok := manager.(DiagnosticsManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		if r.URL.RawQuery != "" {
			write(w, http.StatusBadRequest, Error{Code: "bad_request", Message: "unexpected query"})
			return
		}
		value, err := p.Diagnostics(r.Context(), agentapi.AppKey{Project: r.PathValue("project"), Name: r.PathValue("name")})
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/logs/{project}/{name}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		p, ok := manager.(DiagnosticsManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		tail := uint64(100)
		if q := r.URL.RawQuery; q != "" {
			var err error
			if !strings.HasPrefix(q, "tail=") {
				err = errors.New("invalid tail")
			} else {
				tail, err = strconv.ParseUint(strings.TrimPrefix(q, "tail="), 10, 16)
			}
			if err != nil || tail < 1 || tail > 500 {
				write(w, http.StatusBadRequest, Error{Code: "bad_request", Message: "tail must be 1..500"})
				return
			}
		}
		value, err := p.Logs(r.Context(), agentapi.AppKey{Project: r.PathValue("project"), Name: r.PathValue("name")}, uint16(tail))
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/secrets", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodPost) {
			return
		}
		var input agentapi.SecretInput
		if !decode(w, r, &input) {
			return
		}
		p, ok := manager.(ResourceManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusAccepted, "")
			return
		}
		value, err := p.CreateSecret(r.Context(), input)
		respond(w, value, err, http.StatusAccepted, "")
	})
	mux.HandleFunc("/api/v1/secrets/{project}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		p, ok := manager.(ResourceManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		value, err := p.Secrets(r.Context(), r.PathValue("project"))
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/volumes/{project}/{name}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		p, ok := manager.(ResourceManager)
		if !ok {
			respond(w, nil, &agentapi.RequestError{}, http.StatusOK, "")
			return
		}
		value, err := p.Volumes(r.Context(), agentapi.AppKey{Project: r.PathValue("project"), Name: r.PathValue("name")})
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		value, err := manager.Health(r.Context())
		if err == nil && (value.APIVersion != agentapi.APIVersion || !value.Accepting) {
			err = &agentapi.RequestError{}
		}
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/apps", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		value, err := manager.Applications(r.Context())
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/apps/apply", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodPost) {
			return
		}
		var request ApplyRequest
		if !decode(w, r, &request) {
			return
		}
		wait := uint64(1000)
		if request.StartupWaitMS != nil {
			wait = *request.StartupWaitMS
		}
		value, err := manager.Apply(r.Context(), request.RequestID, request.Spec, wait)
		respond(w, value, err, http.StatusAccepted, request.RequestID)
	})
	mux.HandleFunc("/api/v1/apps/{project}/{name}/{action}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodPost) {
			return
		}
		action := r.PathValue("action")
		if action != "start" && action != "stop" {
			write(w, http.StatusNotFound, Error{Code: "not_found", Message: "endpoint not found"})
			return
		}
		var request IntentRequest
		if !decode(w, r, &request) {
			return
		}
		app := agentapi.AppKey{Project: r.PathValue("project"), Name: r.PathValue("name")}
		value, err := manager.Intent(r.Context(), request.RequestID, app, action)
		respond(w, value, err, http.StatusAccepted, request.RequestID)
	})
	mux.HandleFunc("/api/v1/jobs/{id}", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
		if err != nil || id < 1 {
			write(w, http.StatusBadRequest, Error{Code: "bad_request", Message: "invalid job ID"})
			return
		}
		value, err := manager.Job(r.Context(), id)
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/api/v1/operations", func(w http.ResponseWriter, r *http.Request) {
		if !method(w, r, http.MethodGet) {
			return
		}
		query, err := parseLimit(r)
		if err != nil {
			write(w, http.StatusBadRequest, Error{Code: "bad_request", Message: "invalid limit"})
			return
		}
		value, err := manager.Operations(r.Context(), query)
		respond(w, value, err, http.StatusOK, "")
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		write(w, http.StatusNotFound, Error{Code: "not_found", Message: "endpoint not found"})
	})
	slots := make(chan struct{}, 16)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		auth := r.Header.Get("Authorization")
		given := sha256.Sum256([]byte(strings.TrimPrefix(auth, "Bearer ")))
		if !strings.HasPrefix(auth, "Bearer ") || subtle.ConstantTimeCompare(given[:], expected[:]) != 1 {
			w.Header().Set("WWW-Authenticate", "Bearer")
			write(w, http.StatusUnauthorized, Error{Code: "unauthorized", Message: "valid bearer token required"})
			return
		}
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		default:
			write(w, http.StatusServiceUnavailable, Error{Code: "busy", Message: "control plane is busy", Retryable: true})
			return
		}
		mux.ServeHTTP(w, r)
	}), nil
}
func parseLimit(r *http.Request) (uint16, error) {
	if r.URL.RawQuery == "" {
		return 20, nil
	}
	query, err := url.ParseQuery(r.URL.RawQuery)
	values := query["limit"]
	if err != nil || len(query) != 1 || len(values) != 1 {
		return 0, errors.New("invalid query")
	}
	n, err := strconv.ParseUint(values[0], 10, 16)
	if err != nil || n < 1 || n > 1000 {
		return 0, errors.New("invalid limit")
	}
	return uint16(n), nil
}
func method(w http.ResponseWriter, r *http.Request, expected string) bool {
	if r.Method == expected {
		return true
	}
	w.Header().Set("Allow", expected)
	write(w, http.StatusMethodNotAllowed, Error{Code: "method_not_allowed", Message: "method not allowed"})
	return false
}
func decode(w http.ResponseWriter, r *http.Request, value any) bool {
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		write(w, http.StatusUnsupportedMediaType, Error{Code: "bad_request", Message: "Content-Type must be application/json"})
		return false
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, agentapi.MaxRequestBytes))
	if err != nil {
		status := http.StatusBadRequest
		var large *http.MaxBytesError
		if errors.As(err, &large) {
			status = http.StatusRequestEntityTooLarge
		}
		write(w, status, Error{Code: "bad_request", Message: "invalid or oversized request body"})
		return false
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if !utf8.Valid(data) || decoder.Decode(value) != nil || decoder.Decode(new(any)) != io.EOF {
		write(w, http.StatusBadRequest, Error{Code: "bad_request", Message: "invalid JSON request"})
		return false
	}
	return true
}
func respond(w http.ResponseWriter, value any, err error, status int, key string) {
	if err == nil {
		write(w, status, value)
		return
	}
	detail := Error{Code: "internal", Message: "control plane request failed"}
	status = http.StatusInternalServerError
	var validation *agentapi.ValidationError
	var upstream *agentapi.HTTPError
	var transport *agentapi.RequestError
	switch {
	case errors.As(err, &validation):
		status = http.StatusBadRequest
		detail = Error{Code: "bad_request", Message: validation.Error()}
	case errors.As(err, &upstream):
		// Preserve Agent's stable public error envelope, not raw transport diagnostics.
		status = upstream.Status
		if status < 400 || status > 599 {
			status = http.StatusBadGateway
		}
		detail = Error{Code: upstream.Detail.Code, Message: upstream.Detail.Message, Retryable: upstream.Detail.Retryable}
	case errors.Is(err, agentapi.ErrProtocol):
		status = http.StatusBadGateway
		detail = Error{Code: "agent_protocol", Message: "invalid or incompatible Agent response"}
	case errors.As(err, &transport), errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
		status = http.StatusServiceUnavailable
		detail = Error{Code: "agent_unavailable", Message: "Agent unavailable; a submission may already be accepted; retry only with the same request_id and command", Retryable: true}
		if key == "" {
			detail.Message = "Agent unavailable; refresh current state before retrying the operation"
		}
	}
	// Only include syntactically safe keys; errors must not echo arbitrary input values.
	if agentapi.ValidateRequestID(key) == nil {
		detail.RequestID = key
	}
	write(w, status, detail)
}
func write(w http.ResponseWriter, status int, value any) {
	data, err := json.Marshal(value)
	if err != nil || len(data) > agentapi.MaxResponseBytes {
		status = http.StatusBadGateway
		data = []byte(`{"code":"agent_protocol","message":"invalid or oversized response","retryable":false}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(data)
}
