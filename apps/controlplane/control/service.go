// Package control coordinates user commands through an Agent port only.
// Agent remains the sole configuration store, executor and recovery owner.
package control

import (
	"context"

	"deploy-platform.local/agentapi"
	"deploy-platform.local/controlplane/certificates"
)

type Agent interface {
	Health(context.Context) (agentapi.Health, error)
	Applications(context.Context) ([]agentapi.Application, error)
	Operations(context.Context, uint16) ([]agentapi.Operation, error)
	Submit(context.Context, agentapi.Submission) (agentapi.Job, error)
	Job(context.Context, int64) (agentapi.Job, error)
}
type Service struct {
	agent            Agent
	certificateSlots chan struct{}
}

func New(agent Agent) *Service {
	return &Service{agent: agent, certificateSlots: make(chan struct{}, 2)}
}
func (s *Service) Health(ctx context.Context) (agentapi.Health, error) { return s.agent.Health(ctx) }
func (s *Service) Applications(ctx context.Context) ([]agentapi.Application, error) {
	return s.agent.Applications(ctx)
}
func (s *Service) Operations(ctx context.Context, limit uint16) ([]agentapi.Operation, error) {
	if limit < 1 || limit > 1000 {
		return nil, &agentapi.ValidationError{Field: "limit; expected 1..1000"}
	}
	return s.agent.Operations(ctx, limit)
}
func (s *Service) Job(ctx context.Context, id int64) (agentapi.Job, error) {
	if id < 1 {
		return agentapi.Job{}, &agentapi.ValidationError{Field: "job ID"}
	}
	return s.agent.Job(ctx, id)
}
func (s *Service) Apply(ctx context.Context, key string, spec agentapi.AppSpec, waitMS uint64) (agentapi.Job, error) {
	return s.submit(ctx, agentapi.Submission{RequestID: key, Command: agentapi.Command{Kind: "apply", Spec: &spec, StartupWaitMS: &waitMS}})
}
func (s *Service) Intent(ctx context.Context, key string, app agentapi.AppKey, kind string) (agentapi.Job, error) {
	if kind != "start" && kind != "stop" {
		return agentapi.Job{}, &agentapi.ValidationError{Field: "intent action"}
	}
	return s.submit(ctx, agentapi.Submission{RequestID: key, Command: agentapi.Command{Kind: kind, App: &app}})
}
func (s *Service) submit(ctx context.Context, submission agentapi.Submission) (agentapi.Job, error) {
	if err := submission.Validate(); err != nil {
		return agentapi.Job{}, err
	}
	// Do not generate a new ID or retry on behalf of the caller after transport failure.
	return s.agent.Submit(ctx, submission)
}

// ResourcePort is optional for other Agent implementations, separate from task execution.
type ResourcePort interface {
	CreateSecret(context.Context, agentapi.SecretInput) (agentapi.SecretInfo, error)
	Secrets(context.Context, string) ([]agentapi.SecretInfo, error)
	Volumes(context.Context, agentapi.AppKey) ([]agentapi.VolumeInfo, error)
}

func (s *Service) CreateSecret(ctx context.Context, input agentapi.SecretInput) (agentapi.SecretInfo, error) {
	if err := input.Validate(); err != nil {
		return agentapi.SecretInfo{}, err
	}
	p, ok := s.agent.(ResourcePort)
	if !ok {
		return agentapi.SecretInfo{}, &agentapi.RequestError{}
	}
	return p.CreateSecret(ctx, input)
}
func (s *Service) Secrets(ctx context.Context, project string) ([]agentapi.SecretInfo, error) {
	if err := (agentapi.AppKey{Project: project, Name: "check"}).Validate(); err != nil {
		return nil, err
	}
	p, ok := s.agent.(ResourcePort)
	if !ok {
		return nil, &agentapi.RequestError{}
	}
	return p.Secrets(ctx, project)
}
func (s *Service) Volumes(ctx context.Context, app agentapi.AppKey) ([]agentapi.VolumeInfo, error) {
	if err := app.Validate(); err != nil {
		return nil, err
	}
	p, ok := s.agent.(ResourcePort)
	if !ok {
		return nil, &agentapi.RequestError{}
	}
	return p.Volumes(ctx, app)
}

// DiagnosticsPort keeps read-only runtime inspection independent from deployment commands.
type DiagnosticsPort interface {
	Diagnostics(context.Context, agentapi.AppKey) (agentapi.Diagnostics, error)
	Logs(context.Context, agentapi.AppKey, uint16) (agentapi.LogSnapshot, error)
}

func (s *Service) Diagnostics(ctx context.Context, app agentapi.AppKey) (agentapi.Diagnostics, error) {
	if err := app.Validate(); err != nil {
		return agentapi.Diagnostics{}, err
	}
	p, ok := s.agent.(DiagnosticsPort)
	if !ok {
		return agentapi.Diagnostics{}, &agentapi.RequestError{}
	}
	return p.Diagnostics(ctx, app)
}
func (s *Service) Logs(ctx context.Context, app agentapi.AppKey, tail uint16) (agentapi.LogSnapshot, error) {
	if err := app.Validate(); err != nil {
		return agentapi.LogSnapshot{}, err
	}
	if tail < 1 || tail > 500 {
		return agentapi.LogSnapshot{}, &agentapi.ValidationError{Field: "tail"}
	}
	p, ok := s.agent.(DiagnosticsPort)
	if !ok {
		return agentapi.LogSnapshot{}, &agentapi.RequestError{}
	}
	return p.Logs(ctx, app, tail)
}

type IngressPort interface {
	Routes(context.Context) (agentapi.IngressSnapshot, error)
	ChangeRoute(context.Context, agentapi.RouteChange) (agentapi.RouteReceipt, error)
	DeleteRoute(context.Context, agentapi.RouteDelete) (agentapi.RouteReceipt, error)
}

func (s *Service) Routes(ctx context.Context) (agentapi.IngressSnapshot, error) {
	p, ok := s.agent.(IngressPort)
	if !ok {
		return agentapi.IngressSnapshot{}, &agentapi.RequestError{}
	}
	return p.Routes(ctx)
}
func (s *Service) ChangeRoute(ctx context.Context, input agentapi.RouteChange) (agentapi.RouteReceipt, error) {
	if err := input.Route.Validate(); err != nil {
		return agentapi.RouteReceipt{}, err
	}
	if input.ExpectedRevision < 0 {
		return agentapi.RouteReceipt{}, &agentapi.ValidationError{Field: "revision"}
	}
	p, ok := s.agent.(IngressPort)
	if !ok {
		return agentapi.RouteReceipt{}, &agentapi.RequestError{}
	}
	return p.ChangeRoute(ctx, input)
}
func (s *Service) DeleteRoute(ctx context.Context, input agentapi.RouteDelete) (agentapi.RouteReceipt, error) {
	if !agentapi.ValidDomain(input.Domain) || input.ExpectedRevision < 1 {
		return agentapi.RouteReceipt{}, &agentapi.ValidationError{Field: "domain or revision"}
	}
	p, ok := s.agent.(IngressPort)
	if !ok {
		return agentapi.RouteReceipt{}, &agentapi.RequestError{}
	}
	return p.DeleteRoute(ctx, input)
}
func (s *Service) Certificate(ctx context.Context, domain string) (agentapi.CertificateStatus, error) {
	select {
	case s.certificateSlots <- struct{}{}:
		defer func() { <-s.certificateSlots }()
	default:
		return agentapi.CertificateStatus{}, &agentapi.RequestError{}
	}

	if !agentapi.ValidDomain(domain) {
		return agentapi.CertificateStatus{}, &agentapi.ValidationError{Field: "domain"}
	}
	snapshot, err := s.Routes(ctx)
	if err != nil {
		return agentapi.CertificateStatus{}, err
	}
	for _, item := range snapshot.Routes {
		if item.Route.Domain == domain && item.Route.Enabled {
			return (certificates.Checker{}).Check(ctx, domain, item.Route.TLS), nil
		}
	}
	return agentapi.CertificateStatus{}, &agentapi.HTTPError{Status: 404, Detail: agentapi.APIError{Code: "not_found", Message: "enabled domain binding not found"}}
}
