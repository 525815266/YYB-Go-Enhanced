package version

import (
	"os"
	"strings"
	"testing"
)

func TestSourceAndDockerVersionMatchRelease(t *testing.T) {
	contents, err := os.ReadFile("../../VERSION")
	if err != nil {
		// Docker builds receive the version via build arguments, not the source file.
		if os.IsNotExist(err) {
			t.Skip("VERSION not included in build context")
		}
		t.Fatal(err)
	}
	want := strings.TrimSpace(string(contents))
	if Version != want {
		t.Fatalf("source default version %s differs from VERSION %s", Version, want)
	}
	docker, err := os.ReadFile("../../Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(docker), "ARG VERSION="+want+"\n") != 2 && strings.Count(string(docker), "ARG VERSION="+want+"\r\n") != 2 {
		t.Fatal("Docker default version must match VERSION for both build and runtime stages")
	}
}
