// Package certificates observes the certificate actually served by the local proxy.
// It never connects to a user-supplied host or reads/export private keys.
package certificates

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"deploy-platform.local/agentapi"
	"net"
	"time"
)

// Checker permits a trusted test/composition root to override address and roots.
// HTTP callers supply only a validated, registered DNS name.
type Checker struct {
	Address string
	Roots   *x509.CertPool
}

func (c Checker) Check(ctx context.Context, domain, mode string) agentapi.CertificateStatus {
	result := agentapi.CertificateStatus{Domain: domain, State: "pending", CheckedAtMS: time.Now().UnixMilli()}
	if !agentapi.ValidDomain(domain) {
		result.State = "invalid"
		return result
	}
	address := c.Address
	if address == "" {
		address = "127.0.0.1:443"
	}
	ctx, cancel := context.WithTimeout(ctx, 1500*time.Millisecond)
	defer cancel()
	// First inspect the presented chain; trust, hostname and expiry are verified below.
	dialer := tls.Dialer{NetDialer: &net.Dialer{Timeout: time.Second}, Config: &tls.Config{ServerName: domain, InsecureSkipVerify: true, MinVersion: tls.VersionTLS12}}
	connection, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return result
	}
	defer connection.Close()
	state := connection.(*tls.Conn).ConnectionState()
	if len(state.PeerCertificates) == 0 {
		return result
	}
	leaf := state.PeerCertificates[0]
	expiry := leaf.NotAfter.UTC().Format(time.RFC3339)
	issuer := leaf.Issuer.CommonName
	result.NotAfter = &expiry
	result.Issuer = &issuer
	if err := leaf.VerifyHostname(domain); err != nil {
		result.State = "hostname_mismatch"
		return result
	}
	if time.Now().Before(leaf.NotBefore) || !time.Now().Before(leaf.NotAfter) {
		result.State = "expired_or_not_yet_valid"
		return result
	}
	intermediates := x509.NewCertPool()
	for _, cert := range state.PeerCertificates[1:] {
		intermediates.AddCert(cert)
	}
	_, err = leaf.Verify(x509.VerifyOptions{DNSName: domain, Roots: c.Roots, Intermediates: intermediates})
	result.Trusted = err == nil
	if mode == "staging" {
		result.State = "staging"
	} else if result.Trusted {
		result.State = "valid"
	} else {
		result.State = "untrusted"
	}
	return result
}
