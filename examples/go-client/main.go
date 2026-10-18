// Small direct Agent client using the reusable local SDK. Run from the workspace root:
// go run examples/go-client/main.go -socket /run/deploy-platform/agent.sock
// -submit expects an Agent Submission, not a control-plane ApplyRequest.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"deploy-platform.local/agentapi"
)

func run() error {
	socket := flag.String("socket", "/run/deploy-platform/agent.sock", "Agent Unix Socket")
	submit := flag.String("submit", "", "optional JSON Submission file with stable request_id")
	job := flag.Int64("job", 0, "optional accepted job ID")
	flag.Parse()
	client, err := agentapi.NewClient(*socket, 5*time.Second)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var result any
	if *submit != "" {
		file, err := os.Open(*submit)
		if err != nil {
			return err
		}
		defer file.Close()
		data, err := io.ReadAll(io.LimitReader(file, agentapi.MaxRequestBytes+1))
		if err != nil || len(data) > agentapi.MaxRequestBytes {
			return fmt.Errorf("cannot read bounded submission")
		}
		var request agentapi.Submission
		if json.Unmarshal(data, &request) != nil {
			return fmt.Errorf("invalid JSON submission")
		}
		result, err = client.Submit(ctx, request)
		if err != nil {
			return fmt.Errorf("%w; retry only with the original request_id and command", err)
		}
	} else if *job > 0 {
		result, err = client.Job(ctx, *job)
	} else {
		result, err = client.Health(ctx)
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
