package notifications

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func sample() Snapshot {
	return Snapshot{Revision: 1, Latest: map[string]Notice{"dev": {Repository: "stewardful/deploy-platform", Channel: "dev", Tag: "dev-42", ReleaseID: 1, PublishedAt: time.Now().UTC()}}}
}
func TestEndpointIsLiteralLoopbackWithoutProxyRedirectOrCredentials(t *testing.T) {
	for _, url := range []string{"https://127.0.0.1:8090/v1/events", "http://localhost:8090/v1/events", "http://example.com:8090/v1/events", "http://169.254.169.254:80/v1/events", "http://user:pass@127.0.0.1:8090/v1/events", "http://127.0.0.1:8090/v1/events?q=1", "http://127.0.0.1:8090/other", "http://127.0.0.1/v1/events"} {
		if _, err := New(url); err == nil {
			t.Fatal("unsafe endpoint accepted", url)
		}
	}
	if _, err := New(DefaultEndpoint); err != nil {
		t.Fatal(err)
	}
}
func TestStreamHandlesHeartbeatAndSnapshotWithoutAuthentication(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.URL.Path != "/v1/events" {
			t.Error("wrong transport")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		data, _ := json.Marshal(sample())
		fmt.Fprintf(w, ": heartbeat\n\nid: 1\nevent: snapshot\ndata: %s\n\n", data)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	client, _ := New(server.URL + "/v1/events")
	count := 0
	_ = client.Stream(ctx, func(s Snapshot) {
		count++
		if s.Latest["dev"].Tag != "dev-42" {
			t.Error(s)
		}
		cancel()
	})
	if count != 1 {
		t.Fatal("snapshot lost")
	}
}
func TestMalformedOversizedOrUntrustedFramesCannotNotify(t *testing.T) {
	for _, body := range []string{"event: snapshot\ndata: {}\n\n", "event: snapshot\ndata: {\"latest\":{}}\n\n", "event: snapshot\ndata: {\"revision\":0,\"latest\":{},\"command\":\"sh\"}\n\n", "event: snapshot\ndata: {\"revision\":0,\"latest\":{\"evil\":{}}}\n\n", "data: " + strings.Repeat("x", maxFrame+1) + "\n", strings.Repeat("id: 1\n", maxFrame)} {
		called := false
		err := decode(context.Background(), strings.NewReader(body), func(Snapshot) { called = true })
		if err == nil || called {
			t.Fatal("invalid snapshot accepted")
		}
	}
	for _, status := range []int{302, 500} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Location", "http://169.254.169.254/")
			w.WriteHeader(status)
		}))
		client, _ := New(server.URL + "/v1/events")
		if err := client.Stream(context.Background(), func(Snapshot) { t.Error("invalid response notified") }); err == nil {
			t.Fatal("bad response accepted")
		}
		server.Close()
	}
}
func TestNoticeMetadataCannotBecomeAnExecutableInstruction(t *testing.T) {
	for _, tag := range []string{"dev-0", "dev-42;sh", "dev-99999999999999999999", "https://evil/installer"} {
		s := sample()
		n := s.Latest["dev"]
		n.Tag = tag
		s.Latest["dev"] = n
		if ValidSnapshot(s) {
			t.Fatal("invalid tag", tag)
		}
	}
}
