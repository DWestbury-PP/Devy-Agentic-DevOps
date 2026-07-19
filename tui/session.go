package main

import (
	"os"
	"path/filepath"
	"strings"
)

// The server-issued session id is the source of truth for a conversation; every
// surface (web, TUI, …) resumes by naming it. `--session <id>` names it
// explicitly. `--continue` is convenience sugar: it caches the last id THIS CLI
// received in a local state file and re-uses it, so you don't paste the hash. It
// is per-machine/per-account and never a cross-surface "latest conversation".

// sessionStatePath follows the project's ~/.config/agentic-devops/ convention
// (honoring XDG_CONFIG_HOME).
func sessionStatePath() string {
	dir := os.Getenv("XDG_CONFIG_HOME")
	if dir == "" {
		dir = filepath.Join(os.Getenv("HOME"), ".config")
	}
	return filepath.Join(dir, "agentic-devops", "last-session")
}

// readLastSession returns the cached last session id, or "" if none/unreadable.
func readLastSession() string {
	b, err := os.ReadFile(sessionStatePath())
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// saveLastSession caches id for `--continue`. Empty ids are a no-op so a
// stateless turn never clobbers a good cached id.
func saveLastSession(id string) {
	if id == "" {
		return
	}
	p := sessionStatePath()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return
	}
	_ = os.WriteFile(p, []byte(id+"\n"), 0o600)
}

// resolveStartSession picks the conversation to resume: an explicit id wins,
// then --continue (the last id this CLI saw), else "" (start fresh). lastFn is
// injected for testability.
func resolveStartSession(explicit string, cont bool, lastFn func() string) string {
	if explicit != "" {
		return explicit
	}
	if cont {
		return lastFn()
	}
	return ""
}
