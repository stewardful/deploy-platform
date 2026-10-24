package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"deploy-platform.local/agentapi"
	"deploy-platform.local/controlplane/config"
	"deploy-platform.local/controlplane/control"
	"deploy-platform.local/controlplane/httpapi"
	"deploy-platform.local/controlplane/updates"
	"deploy-platform.local/controlplane/webui"
)

func run() error {
	cfg, err := config.Load(os.Args[1:], os.Stderr)
	if err != nil {
		return err
	}
	client, err := agentapi.NewClient(cfg.AgentSocket, cfg.AgentTimeout)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var updater httpapi.Updater
	var webhook http.Handler
	if cfg.UpdatesDir != "" {
		service, err := updates.New(cfg.UpdatesDir, cfg.ReleaseManifest, cfg.UpdatesConfig)
		if err != nil {
			return err
		}
		updater = service
		webhook = service.WebhookHandler()
		go service.Run(ctx)
	}
	handler, err := httpapi.NewWithUpdates(control.New(client), cfg.Token, updater)
	if err != nil {
		return err
	}
	handler, err = webui.New(handler, cfg.WebDir)
	if err != nil {
		return err
	}
	if webhook != nil {
		site := handler
		handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/hooks/github/releases" {
				webhook.ServeHTTP(w, r)
				return
			}
			site.ServeHTTP(w, r)
		})
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "tcp", cfg.Listen)
	if err != nil {
		return fmt.Errorf("cannot bind control listener: %w", err)
	}
	server := &http.Server{
		Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: cfg.AgentTimeout + 5*time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 16 * 1024,
		BaseContext: func(net.Listener) context.Context { return ctx },
	}
	result := make(chan error, 1)
	go func() { result <- server.Serve(listener) }()
	slog.Info("control plane started", "listen", cfg.Listen)
	select {
	case err := <-result:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdown); err != nil {
			_ = server.Close()
			return errors.New("control server shutdown deadline exceeded")
		}
		if err := <-result; !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	}
	slog.Info("control plane stopped; Agent and application jobs continue independently")
	return nil
}
func main() {
	if err := run(); err != nil && !errors.Is(err, flag.ErrHelp) {
		slog.Error("control plane stopped", "error", err)
		os.Exit(1)
	}
}
