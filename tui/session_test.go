package main

import "testing"

func TestResolveStartSessionPrecedence(t *testing.T) {
	last := func() string { return "LAST" }

	if got := resolveStartSession("EXPLICIT", true, last); got != "EXPLICIT" {
		t.Errorf("explicit --session must win over --continue, got %q", got)
	}
	if got := resolveStartSession("", true, last); got != "LAST" {
		t.Errorf("--continue must resume the last id, got %q", got)
	}
	if got := resolveStartSession("", false, last); got != "" {
		t.Errorf("no flags must start fresh, got %q", got)
	}
}

func TestLastSessionRoundTrip(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())

	if got := readLastSession(); got != "" {
		t.Fatalf("expected no cached session initially, got %q", got)
	}
	saveLastSession("sess-abc123")
	if got := readLastSession(); got != "sess-abc123" {
		t.Fatalf("round-trip failed: got %q, want sess-abc123", got)
	}
	// An empty id (e.g. a stateless --complete turn) must not clobber the cache.
	saveLastSession("")
	if got := readLastSession(); got != "sess-abc123" {
		t.Fatalf("empty save clobbered the cache: got %q", got)
	}
}
