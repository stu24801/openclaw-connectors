# docker-portforward

A zero-dependency Python TCP port-forwarder that makes host `127.0.0.1` services
reachable **inside Docker containers** via the bridge gateway IP.

## Problem

When a service (e.g. `llm-proxy`) binds only to `127.0.0.1` on the host,
Docker containers cannot reach it — even with `extra_hosts: host.docker.internal`.
This tool bridges the gap by binding on the Docker bridge gateway (e.g. `172.18.0.1`)
and forwarding traffic to `127.0.0.1` on the host.

```
Docker container
    └─► 172.18.0.1:9090   ← portforward (this tool)
             └─► 127.0.0.1:9000  ← llm-proxy / any local service
```

## Quick start

```bash
# One-shot (foreground)
python3 portforward.py \
  --listen-host 172.18.0.1 \
  --listen-port 9090 \
  --dst-host    127.0.0.1 \
  --dst-port    9000
```

## Find your Docker bridge gateway IP

```bash
docker network inspect <network-name> --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}'
# e.g. → 172.18.0.1
```

## Install as a persistent systemd user service

```bash
# 1. Copy the script
cp portforward.py ~/portforward-llm.py

# 2. Create config dir and copy the env file
mkdir -p ~/.config/portforward
cp llm-proxy.env.example ~/.config/portforward/llm-proxy.env
# Edit the file if your IPs/ports differ

# 3. Install the service template
mkdir -p ~/.config/systemd/user
cp portforward@.service ~/.config/systemd/user/

# 4. Enable and start (the instance name matches the .env filename)
systemctl --user daemon-reload
systemctl --user enable --now portforward@llm-proxy.service

# 5. Verify
systemctl --user status portforward@llm-proxy.service
```

## Configure banana-slides to use the forwarded port

After the forwarder is running, update the API base URL via the settings API:

```bash
curl -X PUT http://localhost:3001/api/settings \
  -H "Content-Type: application/json" \
  -d '{"api_base_url": "http://172.18.0.1:9090/v1"}'
```

Or set it in the web UI at `/settings` → API Base URL.

## Arguments

| Flag | Default | Description |
|---|---|---|
| `--listen-host` | `172.18.0.1` | IP to bind (Docker bridge gateway) |
| `--listen-port` | `9090` | Port Docker containers connect to |
| `--dst-host` | `127.0.0.1` | Destination host |
| `--dst-port` | `9000` | Destination port (your local service) |
