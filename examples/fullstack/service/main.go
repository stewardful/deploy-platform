// A small deployment fixture, built off-node. Both roles resolve service names
// through the runtime-managed hosts file; no Docker API or host path access.
package main

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"

	_ "github.com/lib/pq"
	"github.com/redis/go-redis/v9"
)

//go:embed index.html
var index string

func secret(name string) string {
	b, err := os.ReadFile("/run/secrets/" + name)
	if err != nil {
		log.Fatal("required secret unavailable")
	}
	return strings.TrimSpace(string(b))
}
func env(key, fallback string) string {
	if s := os.Getenv(key); s != "" {
		return s
	}
	return fallback
}
func main() {
	if len(os.Args) == 3 && os.Args[1] == "--probe" {
		client := &http.Client{Timeout: 2 * time.Second, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		response, err := client.Get(os.Args[2])
		if err != nil {
			os.Exit(1)
		}
		defer response.Body.Close()
		if response.StatusCode != 200 {
			os.Exit(1)
		}
		fmt.Println("ok")
		return
	}
	mux := http.NewServeMux()
	if env("ROLE", "backend") == "frontend" {
		upstream, _ := url.Parse("http://backend:8080")
		proxy := httputil.NewSingleHostReverseProxy(upstream)
		// Fresh connections resolve updated /etc/hosts after backend recreation.
		proxy.Transport = &http.Transport{Proxy: nil, DisableKeepAlives: true, ResponseHeaderTimeout: 3 * time.Second}
		proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, _ error) {
			http.Error(w, "backend unavailable", http.StatusBadGateway)
		}
		mux.Handle("/api/", proxy)
		mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "ok") })
		mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/" {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprint(w, index)
		})
	} else {
		dsn := &url.URL{Scheme: "postgres", User: url.UserPassword("postgres", secret("db-password")), Host: "postgres:5432", Path: "app"}
		q := dsn.Query()
		q.Set("sslmode", "disable")
		q.Set("connect_timeout", "2")
		dsn.RawQuery = q.Encode()
		db, err := sql.Open("postgres", dsn.String())
		if err != nil {
			log.Fatal("database configuration invalid")
		}
		defer db.Close()
		db.SetMaxOpenConns(2)
		db.SetMaxIdleConns(1)
		db.SetConnMaxLifetime(time.Minute)
		cache := redis.NewClient(&redis.Options{Addr: "redis:6379", Password: secret("redis-password"), PoolSize: 2, MaxActiveConns: 2, DialTimeout: time.Second, ReadTimeout: time.Second, WriteTimeout: time.Second, MaxRetries: -1})
		defer cache.Close()
		check := func(ctx context.Context) error {
			if err := db.PingContext(ctx); err != nil {
				return err
			}
			return cache.Ping(ctx).Err()
		}
		mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
			ctx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
			defer cancel()
			if check(ctx) != nil {
				http.Error(w, "dependency unavailable", 503)
				return
			}
			fmt.Fprint(w, "ready")
		})
		mux.HandleFunc("/api/visits", func(w http.ResponseWriter, r *http.Request) {
			if r.Method != "GET" && r.Method != "POST" {
				w.WriteHeader(405)
				return
			}
			ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
			defer cancel()
			fail := func() {
				log.Print("request failed: dependency unavailable")
				http.Error(w, "dependency unavailable", 503)
			}
			if check(ctx) != nil {
				fail()
				return
			}
			if _, err := db.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS visits(id integer PRIMARY KEY, count bigint NOT NULL)"); err != nil {
				fail()
				return
			}
			var count int64
			var err error
			if r.Method == "POST" {
				err = db.QueryRowContext(ctx, "INSERT INTO visits VALUES(1,1) ON CONFLICT(id) DO UPDATE SET count=visits.count+1 RETURNING count").Scan(&count)
			} else {
				err = db.QueryRowContext(ctx, "SELECT COALESCE((SELECT count FROM visits WHERE id=1),0)").Scan(&count)
			}
			if err != nil {
				fail()
				return
			}
			if err = cache.Set(ctx, "visits", count, 0).Err(); err != nil {
				fail()
				return
			}
			cached, err := cache.Get(ctx, "visits").Result()
			if err != nil {
				fail()
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{"postgres": count, "redis": cached, "service": "backend"})
			log.Print("request completed: postgres + redis")
		})
	}
	server := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 8192}
	log.Print("service started role=" + env("ROLE", "backend"))
	log.Fatal(server.ListenAndServe())
}
