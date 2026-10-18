// Package config loads administrator-controlled process configuration.
package config

import (
	"errors"
	"flag"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	WebDir       string
	Listen       string
	AgentSocket  string
	AgentTimeout time.Duration
	Token        string
}

func Load(args []string, output io.Writer) (Config, error) {
	var c Config
	var file string
	fs := flag.NewFlagSet("controld", flag.ContinueOnError)
	fs.SetOutput(output)
	fs.StringVar(&c.WebDir, "web-dir", "", "absolute prebuilt Web directory; empty enables API-only mode")
	fs.StringVar(&c.Listen, "listen", "127.0.0.1:8088", "loopback IP:port only; use a secured tunnel for remote access")
	fs.StringVar(&c.AgentSocket, "agent-socket", "/run/deploy-platform/agent.sock", "absolute local Agent Unix Socket path")
	fs.DurationVar(&c.AgentTimeout, "agent-timeout", 5*time.Second, "Agent request deadline (100ms..60s)")
	fs.StringVar(&file, "token-file", "/etc/deploy-platform/control.token", "private 0600 token file; 32..128 ASCII letters/digits/_/-")
	if err := fs.Parse(args); err != nil {
		return c, err
	}
	if fs.NArg() != 0 {
		return c, errors.New("unexpected positional arguments")
	}
	host, port, err := net.SplitHostPort(c.Listen)
	if err != nil {
		return c, errors.New("listen must be a loopback IP and port")
	}
	ip, err := netip.ParseAddr(host)
	n, portErr := strconv.ParseUint(port, 10, 16)
	if err != nil || !ip.IsLoopback() || portErr != nil || n == 0 {
		return c, errors.New("listen must be a loopback IP and port 1..65535")
	}
	if !filepath.IsAbs(c.AgentSocket) || c.AgentTimeout < 100*time.Millisecond || c.AgentTimeout > 60*time.Second {
		return c, errors.New("invalid Agent socket or timeout")
	}
	if c.WebDir != "" && !filepath.IsAbs(c.WebDir) {
		return c, errors.New("web directory must be absolute")
	}
	token, err := readToken(file)
	if err != nil {
		return c, err
	}
	c.Token = token
	return c, nil
}
func readToken(path string) (string, error) {
	if !filepath.IsAbs(path) {
		return "", errors.New("token file path must be absolute")
	}
	before, err := os.Lstat(path)
	if err != nil || !before.Mode().IsRegular() || before.Mode().Perm()&0077 != 0 {
		return "", errors.New("token file must be a private regular file (0600), not a symlink")
	}
	f, err := os.Open(path)
	if err != nil {
		return "", errors.New("cannot open token file")
	}
	defer f.Close()
	after, err := f.Stat()
	if err != nil || !os.SameFile(before, after) || after.Mode().Perm()&0077 != 0 {
		return "", errors.New("token file changed while opening")
	}
	data, err := io.ReadAll(io.LimitReader(f, 1025))
	if err != nil || len(data) > 1024 {
		return "", errors.New("cannot read bounded token file")
	}
	token := strings.TrimSpace(string(data))
	if len(token) < 32 || len(token) > 128 {
		return "", errors.New("token must contain 32..128 ASCII letters/digits/_/-")
	}
	for _, c := range []byte(token) {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-') {
			return "", errors.New("token has invalid characters")
		}
	}
	return token, nil
}
