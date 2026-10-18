package agentapi

import (
	"fmt"
	"strings"
	"testing"
)

func TestSecretsAreRedactedAndResourcePathsAreNotHostPaths(t *testing.T) {
	input := SecretInput{Project: "demo", Name: "password", Value: "do-not-log-me"}
	if input.Validate() != nil {
		t.Fatal("valid secret rejected")
	}
	for _, format := range []string{"%v", "%+v", "%#v"} {
		if strings.Contains(fmt.Sprintf(format, input), input.Value) {
			t.Fatal("secret debug leaked")
		}
	}
	for _, path := range []string{"/etc", "/data/../etc", "/data,ro", "relative", "/data//sub"} {
		if ValidVolumeTarget(path) {
			t.Fatal("unsafe target accepted")
		}
	}
	if !ValidVolumeTarget("/var/lib/postgresql/data") {
		t.Fatal("database path rejected")
	}
}
