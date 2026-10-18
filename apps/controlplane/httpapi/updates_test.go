package httpapi

import (
	"context"
	"testing"

	"deploy-platform.local/controlplane/control"
	"deploy-platform.local/controlplane/updates"
)

type fakeUpdater struct{ calls int }

func (f *fakeUpdater) Status() updates.Status { return updates.Status{Enabled: true, Current: "0.5.1"} }
func (f *fakeUpdater) Check(context.Context) (updates.Status, error) {
	f.calls++
	return f.Status(), nil
}
func (f *fakeUpdater) Upgrade(version string) (updates.Status, error) {
	f.calls++
	if version != "0.5.2" {
		return f.Status(), updates.ErrConflict
	}
	return f.Status(), nil
}
func TestUpdateEndpointsAuthenticateAndRejectExecutableInput(t *testing.T) {
	updater := &fakeUpdater{}
	h, err := NewWithUpdates(control.New(&fakePort{}), testToken, updater)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		method, path, body, token string
		status                    int
	}{
		{"GET", "/api/v1/updates", "", "", 401},
		{"POST", "/api/v1/updates/upgrade", `{"version":"0.5.2"}`, "", 401},
		{"POST", "/api/v1/updates/check", `{"url":"https://evil.example"}`, testToken, 400},
		{"POST", "/api/v1/updates/upgrade", `{"version":"0.5.2","command":"rm"}`, testToken, 400},
		{"GET", "/api/v1/updates/upgrade", "", testToken, 405},
		{"GET", "/api/v1/updates", "", testToken, 200},
	} {
		if got := request(h, tc.method, tc.path, tc.body, tc.token); got.Code != tc.status {
			t.Fatalf("%s: %d %s", tc.path, got.Code, got.Body)
		}
	}
	if updater.calls != 0 {
		t.Fatal("invalid mutation forwarded")
	}
	if got := request(h, "POST", "/api/v1/updates/check", `{}`, testToken); got.Code != 202 {
		t.Fatal(got.Code)
	}
	if got := request(h, "POST", "/api/v1/updates/upgrade", `{"version":"0.5.2"}`, testToken); got.Code != 202 {
		t.Fatal(got.Code)
	}
	if got := request(h, "POST", "/api/v1/updates/upgrade", `{"version":"0.5.3"}`, testToken); got.Code != 409 {
		t.Fatal(got.Code)
	}
}
