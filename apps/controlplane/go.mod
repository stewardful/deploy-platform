module deploy-platform.local/controlplane

go 1.23.0

require deploy-platform.local/agentapi v0.0.0

replace deploy-platform.local/agentapi => ../../packages/go/agentapi
