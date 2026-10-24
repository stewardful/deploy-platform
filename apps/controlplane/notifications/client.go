// Package notifications consumes local release hints, never executable update instructions.
package notifications

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const DefaultEndpoint = "http://127.0.0.1:8090/v1/events"
const maxFrame = 128 * 1024

var ErrStream = errors.New("release notification stream unavailable or invalid")
var repoPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
var devPattern = regexp.MustCompile(`^dev-([1-9][0-9]{0,19})$`)
var stablePattern = regexp.MustCompile(`^v(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})$`)

type Notice struct {
	Repository  string    `json:"repository"`
	Channel     string    `json:"channel"`
	Tag         string    `json:"tag"`
	ReleaseID   int64     `json:"release_id"`
	PublishedAt time.Time `json:"published_at"`
}
type Snapshot struct {
	Revision uint64            `json:"revision"`
	Latest   map[string]Notice `json:"latest"`
}

func ValidNotice(n Notice) bool {
	if !repoPattern.MatchString(n.Repository) || strings.Contains(n.Repository, "..") || len(n.Repository) > 200 || n.ReleaseID <= 0 || n.PublishedAt.IsZero() {
		return false
	}
	if n.Channel == "stable" {
		return stablePattern.MatchString(n.Tag)
	}
	if n.Channel != "dev" {
		return false
	}
	match := devPattern.FindStringSubmatch(n.Tag)
	if match == nil {
		return false
	}
	_, err := strconv.ParseUint(match[1], 10, 64)
	return err == nil
}
func ValidSnapshot(s Snapshot) bool {
	if s.Latest == nil || len(s.Latest) > 2 || (s.Revision == 0 && len(s.Latest) != 0) {
		return false
	}
	for channel, n := range s.Latest {
		if channel != n.Channel || !ValidNotice(n) {
			return false
		}
	}
	return true
}
func ValidateEndpoint(endpoint string) error {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "http" || u.User != nil || u.Path != "/v1/events" || u.RawPath != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return ErrStream
	}
	ip, err := netip.ParseAddr(u.Hostname())
	port, pe := strconv.ParseUint(u.Port(), 10, 16)
	if err != nil || !ip.IsLoopback() || pe != nil || port == 0 {
		return ErrStream
	}
	return nil
}

type Client struct {
	endpoint string
	http     *http.Client
}
type idleConn struct{ net.Conn }

func (c idleConn) Read(p []byte) (int, error) {
	if err := c.SetReadDeadline(time.Now().Add(45 * time.Second)); err != nil {
		return 0, err
	}
	return c.Conn.Read(p)
}
func New(endpoint string) (*Client, error) {
	if err := ValidateEndpoint(endpoint); err != nil {
		return nil, err
	}
	transport := &http.Transport{Proxy: nil, ResponseHeaderTimeout: 5 * time.Second, DisableKeepAlives: true,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			conn, err := (&net.Dialer{Timeout: 3 * time.Second}).DialContext(ctx, network, address)
			if err != nil {
				return nil, err
			}
			return idleConn{conn}, nil
		}}
	return &Client{endpoint: endpoint, http: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func (c *Client) Stream(ctx context.Context, receive func(Snapshot)) error {
	req, err := http.NewRequestWithContext(ctx, "GET", c.endpoint, nil)
	if err != nil {
		return ErrStream
	}
	req.Header.Set("Accept", "text/event-stream")
	response, err := c.http.Do(req)
	if err != nil {
		return ErrStream
	}
	defer response.Body.Close()
	media, _, mediaErr := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if response.StatusCode != 200 || mediaErr != nil || media != "text/event-stream" {
		return ErrStream
	}
	return decode(ctx, response.Body, receive)
}
func decode(ctx context.Context, body io.Reader, receive func(Snapshot)) error {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 4096), maxFrame)
	event := ""
	var data strings.Builder
	frameBytes := 0
	for scanner.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		line := scanner.Text()
		frameBytes += len(line) + 1
		if frameBytes > maxFrame {
			return ErrStream
		}
		if line == "" {
			if event == "snapshot" && data.Len() != 0 {
				var frame struct {
					Revision *uint64           `json:"revision"`
					Latest   map[string]Notice `json:"latest"`
				}
				decoder := json.NewDecoder(bytes.NewReader([]byte(data.String())))
				decoder.DisallowUnknownFields()
				if decoder.Decode(&frame) != nil || decoder.Decode(new(any)) != io.EOF || frame.Revision == nil {
					return ErrStream
				}
				value := Snapshot{Revision: *frame.Revision, Latest: frame.Latest}
				if !ValidSnapshot(value) {
					return ErrStream
				}
				receive(value)
			}
			event = ""
			data.Reset()
			frameBytes = 0
			continue
		}
		// Comments are heartbeats. They cannot accumulate an unbounded frame.
		if strings.HasPrefix(line, ":") {
			frameBytes -= len(line) + 1
			continue
		}
		field, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			event = value
		case "data":
			data.WriteString(value)
			data.WriteByte('\n')
		}
	}
	return ErrStream // EOF, oversized line and transport failure all require reconnect.
}
