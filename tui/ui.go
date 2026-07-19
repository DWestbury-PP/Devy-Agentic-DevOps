package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/charmbracelet/glamour"
	"github.com/charmbracelet/glamour/ansi"
	"github.com/charmbracelet/glamour/styles"
	"github.com/charmbracelet/lipgloss"
)

// Devy terminal theme — emerald primary, teal secondary, amber code, graphite
// neutrals; a cohesive companion to the web surface's emerald/graphite look.
// AdaptiveColor lets the UI chrome track the terminal's light/dark background.
var (
	accentColor    = lipgloss.AdaptiveColor{Light: "#1a7f37", Dark: "#3fb950"} // emerald
	secondaryColor = lipgloss.AdaptiveColor{Light: "#1b7c83", Dark: "#39c5cf"} // teal
	dangerColor    = lipgloss.AdaptiveColor{Light: "#cf222e", Dark: "#f85149"} // red

	promptStyle = lipgloss.NewStyle().Bold(true).Foreground(accentColor)
	toolStyle   = lipgloss.NewStyle().Faint(true).Foreground(secondaryColor)
	dimStyle    = lipgloss.NewStyle().Faint(true)
	errStyle    = lipgloss.NewStyle().Foreground(dangerColor)
)

func strPtr(s string) *string { return &s }
func boolPtr(b bool) *bool    { return &b }

// devyGlamourStyle derives a Glamour style from the stock light/dark base and
// re-themes it. Headings lose the literal "##"/"###" ATX marks (Glamour's stock
// H2/H3 prefixes) in favour of a colored glyph accent — a ▌ bar for H1/H2 and an
// indented ▸ for H3 — with emerald headings plus teal links and amber inline
// code. Rebuilt per call from the unmutated stock config, so it's idempotent.
func devyGlamourStyle() ansi.StyleConfig {
	dark := lipgloss.HasDarkBackground()
	c := styles.LightStyleConfig
	h1, head, link, code, codeBg, rule := "#116329", "#1a7f37", "#1b7c83", "#9a6700", "#eff1f3", "#d0d7de"
	if dark {
		c = styles.DarkStyleConfig
		h1, head, link, code, codeBg, rule = "#56d364", "#3fb950", "#39c5cf", "#e3b341", "#161b22", "#30363d"
	}

	// Headings: drop the ATX marks, use a glyph accent + emerald color.
	c.Heading.Color, c.Heading.Bold = strPtr(head), boolPtr(true)
	c.H1.Prefix, c.H1.Suffix = "▌ ", ""
	c.H1.Color, c.H1.BackgroundColor, c.H1.Bold = strPtr(h1), nil, boolPtr(true)
	c.H2.Prefix, c.H2.Color, c.H2.Bold = "▌ ", strPtr(head), boolPtr(true)
	c.H3.Prefix, c.H3.Color, c.H3.Bold = "  ▸ ", strPtr(head), boolPtr(false)
	c.H4.Prefix, c.H4.Color, c.H4.Bold = "  ▸ ", strPtr(head), boolPtr(false)

	// Links + inline code adopt the theme's secondary/amber accents.
	c.Link.Color, c.Link.Underline = strPtr(link), boolPtr(true)
	c.Code.Color, c.Code.BackgroundColor = strPtr(code), strPtr(codeBg)
	c.HorizontalRule.Color = strPtr(rule)
	return c
}

func renderMarkdown(md string) string {
	r, err := glamour.NewTermRenderer(glamour.WithStyles(devyGlamourStyle()), glamour.WithWordWrap(100))
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
