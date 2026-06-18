package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/charmbracelet/glamour"
	"github.com/charmbracelet/lipgloss"
)

var (
	promptStyle = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("12"))
	toolStyle   = lipgloss.NewStyle().Faint(true).Foreground(lipgloss.Color("6"))
	dimStyle    = lipgloss.NewStyle().Faint(true)
	errStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("9"))
)

func renderMarkdown(md string) string {
	r, err := glamour.NewTermRenderer(glamour.WithAutoStyle(), glamour.WithWordWrap(100))
	if err != nil {
		return md + "\n"
	}
	out, err := r.Render(md)
	if err != nil {
		return md + "\n"
	}
	return out
}

func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}

// spinner is a minimal stderr spinner so streamed answers (stdout) stay pipeable.
type spinner struct {
	mu     sync.Mutex
	status string
	stop   chan struct{}
	done   chan struct{}
}

func newSpinner(status string) *spinner {
	return &spinner{status: status, stop: make(chan struct{}), done: make(chan struct{})}
}

func (s *spinner) start() {
	frames := []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}
	go func() {
		defer close(s.done)
		ticker := time.NewTicker(90 * time.Millisecond)
		defer ticker.Stop()
		i := 0
		for {
			select {
			case <-s.stop:
				return
			case <-ticker.C:
				s.mu.Lock()
				status := s.status
				s.mu.Unlock()
				fmt.Fprintf(os.Stderr, "\r%s %s\033[K", toolStyle.Render(frames[i%len(frames)]), status)
				i++
			}
		}
	}()
}

func (s *spinner) set(status string) {
	s.mu.Lock()
	s.status = status
	s.mu.Unlock()
}

func (s *spinner) stopAndClear() {
	close(s.stop)
	<-s.done
	fmt.Fprint(os.Stderr, "\r\033[K") // clear the spinner line
}

// streamTurn streams one chat turn: spinner + tool activity on stderr, then the
// final Markdown answer rendered to stdout. Returns the (possibly new) session id.
func streamTurn(c *Client, message, sessionID, tier, context string) string {
	var body strings.Builder
	newSession := sessionID

	sp := newSpinner("thinking…")
	sp.start()

	err := c.Chat(message, sessionID, tier, context, func(event string, data map[string]any) {
		switch event {
		case "session":
			if v, ok := data["session_id"].(string); ok {
				newSession = v
			}
		case "delta":
			if t, ok := data["text"].(string); ok {
				body.WriteString(t)
			}
		case "tool_call":
			// Text before a tool call is intermediate narration, not the
			// answer; drop it so the final render stays clean.
			body.Reset()
			sp.set(toolStyle.Render("🔧 " + asString(data["name"])))
		case "tools_found":
			sp.set(toolStyle.Render("🔎 discovering tools…"))
		case "tool_result":
			sp.set(toolStyle.Render("✓ " + asString(data["name"])))
		case "error":
			body.WriteString("\n\n**Error:** " + asString(data["message"]))
		}
	})

	sp.stopAndClear()

	if err != nil {
		fmt.Fprintln(os.Stderr, errStyle.Render("Connection error: "+err.Error()))
		return newSession
	}
	fmt.Print(renderMarkdown(strings.TrimSpace(body.String())))
	return newSession
}
