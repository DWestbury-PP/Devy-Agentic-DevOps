package main

import (
	"strings"
	"testing"
)

// The Devy theme drops Glamour's stock "##"/"###" ATX heading prefixes in favour
// of glyph accents (▌ for H1/H2, indented ▸ for H3). This is the behavioural
// change; it holds regardless of the terminal's color profile.
func TestRenderMarkdownHeadingsUseGlyphAccents(t *testing.T) {
	out := renderMarkdown("## Container Stack\n\nBody text.\n\n### Details\n")

	if strings.Contains(out, "## ") || strings.Contains(out, "### ") {
		t.Fatalf("headings still carry raw ATX marks:\n%q", out)
	}
	if !strings.Contains(out, "▌") {
		t.Fatalf("H2 missing the ▌ glyph accent:\n%q", out)
	}
	if !strings.Contains(out, "▸") {
		t.Fatalf("H3 missing the ▸ glyph accent:\n%q", out)
	}
	// Heading text itself must survive (Glamour emits each word in its own ANSI
	// span, so the words aren't contiguous in the raw output — check separately).
	for _, word := range []string{"Container", "Stack", "Details"} {
		if !strings.Contains(out, word) {
			t.Fatalf("heading text %q was lost:\n%q", word, out)
		}
	}
}

func TestRenderWidthIsBoundedAndCapped(t *testing.T) {
	w := renderWidth()
	if w <= 0 {
		t.Fatalf("renderWidth = %d, want a positive wrap width", w)
	}
	if w > maxRenderWidth {
		t.Fatalf("renderWidth = %d, exceeds cap %d", w, maxRenderWidth)
	}
}

func TestDevyGlamourStyleReThemesHeadings(t *testing.T) {
	c := devyGlamourStyle()
	if c.H2.Prefix != "▌ " {
		t.Errorf("H2 prefix = %q, want %q", c.H2.Prefix, "▌ ")
	}
	if c.H3.Prefix != "  ▸ " {
		t.Errorf("H3 prefix = %q, want %q", c.H3.Prefix, "  ▸ ")
	}
	if c.H2.Color == nil || c.Link.Color == nil || c.Code.Color == nil {
		t.Error("expected heading/link/code colors to be set by the Devy theme")
	}
}
