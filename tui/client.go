package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client is a thin HTTP/SSE client for the LLM-PROXY. It shares no code with
// the proxy — it just speaks its API.
type Client struct {
	base string
	http *http.Client
}

func NewClient(base string) *Client {
	return &Client{base: strings.TrimRight(base, "/"), http: &http.Client{}}
}

func (c *Client) Healthy() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/healthz", nil)
	resp, err := c.http.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

type Tier struct {
	Name  string `json:"name"`
	Label string `json:"label"`
}

func (c *Client) Tiers() ([]Tier, error) {
	resp, err := c.http.Get(c.base + "/v1/tiers")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var tiers []Tier
	return tiers, json.NewDecoder(resp.Body).Decode(&tiers)
}

type Tool struct {
	Name      string `json:"name"`
	Category  string `json:"category"`
	WhenToUse string `json:"when_to_use"`
}

func (c *Client) Tools() ([]Tool, error) {
	resp, err := c.http.Get(c.base + "/v1/tools")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var tools []Tool
	return tools, json.NewDecoder(resp.Body).Decode(&tools)
}

// Complete performs a one-shot, non-streaming completion. It returns the
// rendered Markdown and the session id (non-empty only when a session was
// resumed — /v1/complete is stateless for a fresh call).
func (c *Client) Complete(prompt, sessionID, tier, context string, maxChars int) (string, string, error) {
	payload := map[string]any{"prompt": prompt}
	if sessionID != "" {
		payload["session_id"] = sessionID
	}
	if tier != "" {
		payload["tier"] = tier
	}
	if context != "" {
		payload["context"] = context
	}
	if maxChars > 0 {
		payload["max_chars"] = maxChars
	}
	body, _ := json.Marshal(payload)
	resp, err := c.http.Post(c.base+"/v1/complete", "application/json", bytes.NewReader(body))
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return "", "", fmt.Errorf("proxy %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		Markdown  string `json:"markdown"`
		SessionID string `json:"session_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", "", err
	}
	return out.Markdown, out.SessionID, nil
}

// SSEHandler is called for each server-sent event during a streamed chat turn.
type SSEHandler func(event string, data map[string]any)

// Chat streams a multi-turn chat over SSE, invoking handler for each event.
func (c *Client) Chat(message, sessionID, tier, context string, handler SSEHandler) error {
	payload := map[string]any{"message": message}
	if sessionID != "" {
		payload["session_id"] = sessionID
	}
	if tier != "" {
		payload["tier"] = tier
	}
	if context != "" {
		payload["context"] = context
	}
	body, _ := json.Marshal(payload)

	req, _ := http.NewRequest(http.MethodPost, c.base+"/v1/chat", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	// Client IANA timezone → server does DST-correct local-time conversion.
	if tz := time.Local.String(); tz != "" && tz != "Local" {
		req.Header.Set("X-Client-TZ", tz)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("proxy %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024) // tolerate large tool-result events

	var event string
	var dataLines []string
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" { // blank line dispatches the accumulated event
			if len(dataLines) > 0 {
				var parsed map[string]any
				if json.Unmarshal([]byte(strings.Join(dataLines, "\n")), &parsed) == nil {
					handler(event, parsed)
				}
			}
			event, dataLines = "", nil
			continue
		}
		switch {
		case strings.HasPrefix(line, ":"): // keep-alive comment
			continue
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(line[len("event:"):])
		case strings.HasPrefix(line, "data:"):
			dataLines = append(dataLines, strings.TrimSpace(line[len("data:"):]))
		}
	}
	return scanner.Err()
}
