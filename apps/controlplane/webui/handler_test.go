package webui

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStaticAssetsAndAPIStaySeparate(t *testing.T) {
	dir := t.TempDir()
	_ = os.WriteFile(filepath.Join(dir, "index.html"), []byte("<html>Web login</html>"), 0600)
	_ = os.Mkdir(filepath.Join(dir, "assets"), 0700)
	_ = os.WriteFile(filepath.Join(dir, "assets/app.js"), []byte("console.log('app')"), 0600)
	calls := 0
	api := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(401)
		_, _ = w.Write([]byte(`{"code":"unauthorized"}`))
	})
	h, err := New(api, dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		path     string
		status   int
		contains string
	}{
		{"/", 200, "Web login"}, {"/tasks", 200, "Web login"}, {"/assets/app.js", 200, "console.log"}, {"/assets/missing.js", 404, "404"},
		{"/assets/", 404, "404"}, {"/.env", 404, "404"}, {"/api/v1/apps", 401, "unauthorized"}, {"/api/unknown", 401, "unauthorized"},
	} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", test.path, nil))
		if w.Code != test.status || !strings.Contains(w.Body.String(), test.contains) {
			t.Errorf("%s: %d %s", test.path, w.Code, w.Body.String())
		}
		if test.status == 200 && w.Header().Get("Content-Security-Policy") == "" {
			t.Error("missing browser policy")
		}
	}
	if calls != 2 {
		t.Fatal("API route fell through to SPA")
	}
	_ = os.Symlink(filepath.Join(dir, "index.html"), filepath.Join(dir, "link.html"))
	if _, err := New(api, dir); err == nil {
		t.Fatal("accepted symlink asset")
	}
}
