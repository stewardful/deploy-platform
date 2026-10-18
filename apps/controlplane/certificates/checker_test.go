package certificates

import (
	"context"
	"crypto/x509"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestServedCertificateTrustAndHostnameAreSeparateFromPresence(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	address := strings.TrimPrefix(server.URL, "https://")
	roots := x509.NewCertPool()
	roots.AddCert(server.Certificate())
	checker := Checker{Address: address, Roots: roots}
	value := checker.Check(context.Background(), "example.com", "production")
	if value.State != "valid" || !value.Trusted || value.NotAfter == nil {
		t.Fatalf("valid chain was not verified: %+v", value)
	}
	if value := (Checker{Address: address}).Check(context.Background(), "example.com", "production"); value.State != "untrusted" || value.Trusted {
		t.Fatalf("untrusted cert marked valid: %+v", value)
	}
	if value := checker.Check(context.Background(), "other.example.net", "production"); value.State != "hostname_mismatch" {
		t.Fatalf("mismatched hostname accepted: %+v", value)
	}
	if value := checker.Check(context.Background(), "example.com", "staging"); value.State != "staging" {
		t.Fatalf("staging not distinguished: %+v", value)
	}
	if value := checker.Check(context.Background(), "http://example.com/", "production"); value.State != "invalid" {
		t.Fatal("URL accepted as certificate target")
	}
}
