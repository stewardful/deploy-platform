package agentapi

import "testing"

func TestDomainBindingRejectsURLsAndInjection(t *testing.T) {
	for _, domain := range []string{"http://a.com", "*.a.com", "127.0.0.1", "a.com:443", "a.com/path", "a.com\n", "A.com", "a.local", "a.test", "a..com"} {
		if ValidDomain(domain) {
			t.Fatalf("accepted %q", domain)
		}
	}
	for _, domain := range []string{"app.example.com", "xn--fiqs8s.com", "api.xn--fiqs8s"} {
		if !ValidDomain(domain) {
			t.Fatalf("rejected %q", domain)
		}
	}
}
