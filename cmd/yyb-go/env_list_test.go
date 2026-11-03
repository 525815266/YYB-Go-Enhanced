package main

import (
	"slices"
	"testing"
)

func TestEnvListSplitsOnCommasAndWhiteSpace(t *testing.T) {
	t.Setenv("YYB_TEST_LIST", " wxa,wxb  wxc\n,wxd ")
	if got, want := envList("YYB_TEST_LIST"), []string{"wxa", "wxb", "wxc", "wxd"}; !slices.Equal(got, want) {
		t.Fatalf("envList() = %q, want %q", got, want)
	}
	t.Setenv("YYB_TEST_LIST", "")
	if got := envList("YYB_TEST_LIST"); len(got) != 0 {
		t.Fatalf("an empty value must give no entries, got %q", got)
	}
}
