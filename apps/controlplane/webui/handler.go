// Package webui serves a prebuilt SPA alongside an authenticated control API.
// It needs no Node.js process on the deployment node.
package webui

import (
	"errors"
	"io/fs"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

func New(api http.Handler, directory string) (http.Handler, error) {
	if api == nil {
		return nil, errors.New("API handler required")
	}
	if directory == "" {
		return api, nil
	}
	if !filepath.IsAbs(directory) {
		return nil, errors.New("web directory must be absolute")
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("web directory must be a regular directory")
	}
	// Only administrator-built regular assets may be published, never symlinks.
	err = filepath.WalkDir(directory, func(_ string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if !entry.IsDir() && !entry.Type().IsRegular() {
			return errors.New("web assets must be regular files")
		}
		return nil
	})
	if err != nil {
		return nil, errors.New("cannot validate web assets")
	}
	index := filepath.Join(directory, "index.html")
	if info, err := os.Stat(index); err != nil || !info.Mode().IsRegular() {
		return nil, errors.New("web build is missing index.html")
	}
	files := os.DirFS(directory)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api" || strings.HasPrefix(r.URL.Path, "/api/") {
			api.ServeHTTP(w, r)
			return
		}
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
		w.Header().Set("Cache-Control", "no-cache")
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed", 405)
			return
		}
		for _, part := range strings.Split(r.URL.Path, "/") {
			if strings.HasPrefix(part, ".") || strings.Contains(part, "\\") {
				http.NotFound(w, r)
				return
			}
		}
		name := strings.TrimPrefix(path.Clean(r.URL.Path), "/")
		if name == "" || name == "." {
			http.ServeFile(w, r, index)
			return
		}
		file, err := files.Open(name)
		if err != nil {
			if !strings.Contains(path.Base(name), ".") {
				http.ServeFile(w, r, index)
			} else {
				http.NotFound(w, r)
			}
			return
		}
		defer file.Close()
		stat, err := file.Stat()
		if err != nil || !stat.Mode().IsRegular() {
			http.NotFound(w, r)
			return
		}
		if strings.HasPrefix(name, "assets/") {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		// ServeFile provides ranges, HEAD and conditional requests, without directory listings.
		http.ServeFile(w, r, filepath.Join(directory, filepath.FromSlash(name)))
	}), nil
}
