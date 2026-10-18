package config

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigPrivateTokenAndLoopbackOnly(t *testing.T) {
	file := filepath.Join(t.TempDir(), "token")
	token := strings.Repeat("a", 64)
	if err := os.WriteFile(file, []byte(token+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	args := []string{"--token-file", file}
	value, err := Load(args, io.Discard)
	if err != nil || value.Token != token || value.Listen != "127.0.0.1:8088" {
		t.Fatal("invalid default config", err)
	}
	for _, extra := range [][]string{
		{"--listen", "0.0.0.0:8088"}, {"--listen", "[::]:8088"}, {"--listen", "example.com:8088"},
		{"--listen", "127.0.0.1:0"}, {"--web-dir", "relative"}, {"--agent-socket", "relative.sock"}, {"--agent-timeout", "0s"}, {"unexpected"},
	} {
		if _, err := Load(append(append([]string{}, args...), extra...), io.Discard); err == nil {
			t.Fatalf("accepted %v", extra)
		}
	}
	if err := os.Chmod(file, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(args, io.Discard); err == nil {
		t.Fatal("accepted public token file")
	}
	if err := os.Chmod(file, 0600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(filepath.Dir(file), "link")
	if err := os.Symlink(file, link); err != nil {
		t.Fatal(err)
	}
	if _, err := Load([]string{"--token-file", link}, io.Discard); err == nil {
		t.Fatal("accepted symlink token file")
	}
}
