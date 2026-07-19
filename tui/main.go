// Command ask is the terminal surface for the Agentic DevOps co-pilot.
//
//	ask "is anything unhealthy on this box?"          # one-shot, streamed
//	ask --continue "and what's causing the writes?"    # follow up (same convo)
//	ask --session <id> "…"                             # resume a specific convo
//	kubectl get pods | ask "anything wrong here?"      # piped stdin as context
//	ask                                                # interactive REPL
//
// It is a thin native client: it streams from / posts to the LLM-PROXY and
// renders. All reasoning lives in the proxy. Multi-turn context is server-side;
// each one-shot prints its `session:` id so a follow-up can resume it.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

const defaultURL = "http://127.0.0.1:8765"

func resolveURL(flagURL string) string {
	if flagURL != "" {
		return flagURL
	}
	if env := os.Getenv("AGENTIC_DEVOPS_URL"); env != "" {
		return env
	}
	return defaultURL
}

func readStdinContext() string {
	fi, err := os.Stdin.Stat()
	if err != nil {
		return ""
	}
	if fi.Mode()&os.ModeCharDevice != 0 {
		return "" // a terminal, not a pipe
	}
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func main() {
	var tier, url, sessionID string
	var oneShot, continueSession bool
	var maxChars int

	flag.StringVar(&tier, "tier", "", "model tier (e.g. fast/balanced/deep)")
	flag.StringVar(&tier, "t", "", "model tier (shorthand)")
	flag.StringVar(&url, "url", "", "proxy URL (default "+defaultURL+", or $AGENTIC_DEVOPS_URL)")
	flag.StringVar(&sessionID, "session", "", "resume a conversation by id (from a prior answer's `session:` line)")
	flag.StringVar(&sessionID, "s", "", "resume a session by id (shorthand)")
	flag.BoolVar(&continueSession, "continue", false, "resume the last session started from this CLI")
	flag.BoolVar(&oneShot, "complete", false, "one-shot non-streaming completion")
	flag.BoolVar(&oneShot, "c", false, "one-shot (shorthand)")
	flag.IntVar(&maxChars, "max-chars", 0, "cap answer length (one-shot only)")
	flag.Parse()

	client := NewClient(resolveURL(url))
	context := readStdinContext()
	prompt := strings.TrimSpace(strings.Join(flag.Args(), " "))
	startSession := resolveStartSession(sessionID, continueSession, readLastSession)

	if !client.Healthy() {
		fmt.Fprintln(os.Stderr, errStyle.Render(fmt.Sprintf(
			"No proxy at %s.\nStart it with `docker compose up -d` (or `agentic-devops serve`).",
			resolveURL(url))))
		os.Exit(1)
	}

	if prompt == "" {
		if context != "" {
			fmt.Fprintln(os.Stderr, errStyle.Render("Piped input needs a question, e.g. `… | ask \"what's wrong?\"`"))
			os.Exit(1)
		}
		repl(client, tier, startSession)
		return
	}

	if oneShot {
		md, newID, err := client.Complete(prompt, startSession, tier, context, maxChars)
		if err != nil {
			fmt.Fprintln(os.Stderr, errStyle.Render("Error: "+err.Error()))
			os.Exit(1)
		}
		fmt.Print(renderMarkdown(md))
		saveLastSession(newID)
		announceSession(newID)
		return
	}
	newID := streamTurn(client, prompt, startSession, tier, context)
	saveLastSession(newID)
	announceSession(newID)
}

// announceSession tells the user the conversation's id (on stderr, so stdout
// stays clean/pipeable) so a follow-up can resume it.
func announceSession(id string) {
	if id == "" {
		return
	}
	fmt.Fprintln(os.Stderr, dimStyle.Render("session "+id+`  ·  follow up with: ask --continue "…"`))
}

const helpText = `Commands
  /model <tier>   switch model tier for this session
  /models         list available tiers
  /tools          list available tools
  /new            start a fresh conversation
  /help           show this help
  /exit, /quit    leave
Anything else is sent to the co-pilot.`

func repl(client *Client, tier, startSession string) {
	fmt.Printf("%s — your DevOps & SRE co-pilot. Type %s for commands, %s to quit.\n\n",
		promptStyle.Render("Devy"), dimStyle.Render("/help"), dimStyle.Render("/exit"))

	sessionID := startSession
	if startSession != "" {
		fmt.Println(dimStyle.Render("Resuming session " + startSession + ".\n"))
	}
	currentTier := tier
	reader := bufio.NewReader(os.Stdin)

	for {
		fmt.Print(promptStyle.Render("› "))
		line, err := reader.ReadString('\n')
		if err != nil { // EOF (Ctrl-D)
			fmt.Println()
			return
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}

		if strings.HasPrefix(line, "/") {
			if handleCommand(client, line, &sessionID, &currentTier) {
				return // /exit or /quit
			}
			continue
		}
		sessionID = streamTurn(client, line, sessionID, currentTier, "")
		saveLastSession(sessionID)
	}
}

// handleCommand runs a slash command. Returns true if the REPL should exit.
func handleCommand(client *Client, line string, sessionID, currentTier *string) bool {
	cmd, arg, _ := strings.Cut(strings.TrimPrefix(line, "/"), " ")
	cmd = strings.ToLower(cmd)
	arg = strings.TrimSpace(arg)

	switch cmd {
	case "exit", "quit":
		return true
	case "help":
		fmt.Println(helpText)
	case "new":
		*sessionID = ""
		fmt.Println(dimStyle.Render("Started a fresh conversation."))
	case "models":
		tiers, err := client.Tiers()
		if err != nil {
			fmt.Fprintln(os.Stderr, errStyle.Render("Could not fetch tiers: "+err.Error()))
			break
		}
		for _, t := range tiers {
			marker := ""
			if t.Name == *currentTier {
				marker = " (active)"
			}
			fmt.Printf("  %s — %s%s\n", promptStyle.Render(t.Name), t.Label, dimStyle.Render(marker))
		}
	case "model":
		tiers, err := client.Tiers()
		if err != nil {
			fmt.Fprintln(os.Stderr, errStyle.Render("Could not fetch tiers: "+err.Error()))
			break
		}
		valid := false
		names := make([]string, 0, len(tiers))
		for _, t := range tiers {
			names = append(names, t.Name)
			if t.Name == arg {
				valid = true
			}
		}
		if valid {
			*currentTier = arg
			fmt.Println(dimStyle.Render("Model tier set to ") + promptStyle.Render(arg) + dimStyle.Render("."))
		} else {
			fmt.Fprintln(os.Stderr, errStyle.Render(fmt.Sprintf("Unknown tier %q. Available: %s", arg, strings.Join(names, ", "))))
		}
	case "tools":
		tools, err := client.Tools()
		if err != nil {
			fmt.Fprintln(os.Stderr, errStyle.Render("Could not fetch tools: "+err.Error()))
			break
		}
		for _, t := range tools {
			fmt.Printf("  %s %s — %s\n", promptStyle.Render(t.Name), dimStyle.Render("("+t.Category+")"), t.WhenToUse)
		}
	default:
		fmt.Fprintln(os.Stderr, errStyle.Render("Unknown command /"+cmd+". Try /help."))
	}
	return false
}
