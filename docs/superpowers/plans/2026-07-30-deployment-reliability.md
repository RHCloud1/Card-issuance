# Deployment Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make fresh overseas VPS deployments use reliable container DNS, detect HTTPS port conflicts early, wait for TLS readiness, and present project-owned installation and diagnostic messages in Chinese.

**Architecture:** Keep DNS scoped to this Compose project through environment-backed service configuration rather than changing Docker globally. Exercise `setup.sh` as a black-box command inside temporary fixtures with fake `docker`, `ss`, and `curl` executables, then use the same helpers in production to stop early on port conflicts and report HTTPS readiness accurately.

**Tech Stack:** Bash, Docker Compose YAML, Caddy, curl, GNU coreutils, Git.

## Global Constraints

- Default DNS values are `1.1.1.1` and `8.8.8.8`.
- DNS values remain overridable with `CONTAINER_DNS_PRIMARY` and `CONTAINER_DNS_SECONDARY`.
- Do not modify Docker daemon configuration or any non-project container.
- Do not stop a process that owns TCP port `80` or `443`.
- Do not overwrite an existing deployment's generated `docker-compose.override.yml` from `update.sh`.
- Project-owned prompts and errors are Chinese; third-party command output remains unchanged.
- Tests must not require a real Docker daemon, public DNS, or internet access.

---

### Task 1: Deployment Script Regression Harness

**Files:**
- Create: `tests/scripts/deployment_scripts_test.sh`
- Test: `tests/scripts/deployment_scripts_test.sh`

**Interfaces:**
- Consumes: `scripts/setup.sh`, `docker-compose.yml`
- Produces: a standalone Bash test runner that exits nonzero on the first failed assertion

- [ ] **Step 1: Write the failing DNS generation test**

Create a temporary project fixture, put fake commands first in `PATH`, pipe HTTPS setup answers into `setup.sh`, and assert:

```bash
assert_contains "$fixture/docker-compose.override.yml" '${CONTAINER_DNS_PRIMARY:-1.1.1.1}'
assert_contains "$fixture/docker-compose.override.yml" '${CONTAINER_DNS_SECONDARY:-8.8.8.8}'
assert_contains "$fixture/.env" 'CONTAINER_DNS_PRIMARY=1.1.1.1'
assert_contains "$fixture/.env" 'CONTAINER_DNS_SECONDARY=8.8.8.8'
assert_contains "$repo_root/docker-compose.yml" '${CONTAINER_DNS_PRIMARY:-1.1.1.1}'
```

- [ ] **Step 2: Write the failing port-conflict test**

Configure fake `ss` to report TCP `443` in use while fake Docker reports no existing project Caddy. Assert that setup exits nonzero, prints `端口 443 已被占用`, and never records a `docker compose up` call.

- [ ] **Step 3: Write the existing-project exception test**

Configure fake Docker to report a running `card-issuance-caddy`, leave fake `ss` reporting `80/443`, and assert setup proceeds to Compose generation. This proves rerunning setup does not treat the project's own proxy as a third-party conflict.

- [ ] **Step 4: Write HTTPS readiness tests**

Make fake `curl` return success for one test and failure for another. Assert success prints `部署完成`, while timeout prints `HTTPS 尚未就绪` and `./scripts/diagnose.sh`.

- [ ] **Step 5: Run the tests and verify RED**

Run:

```bash
bash tests/scripts/deployment_scripts_test.sh
```

Expected: FAIL because the current scripts do not generate DNS configuration or preflight ports.

- [ ] **Step 6: Commit the failing regression harness**

```bash
git add tests/scripts/deployment_scripts_test.sh
git commit -m "test: cover deployment reliability failures"
```

### Task 2: DNS Defaults, Port Preflight, and HTTPS Readiness

**Files:**
- Modify: `docker-compose.yml`
- Modify: `scripts/setup.sh`
- Test: `tests/scripts/deployment_scripts_test.sh`

**Interfaces:**
- Consumes: `CONTAINER_DNS_PRIMARY`, `CONTAINER_DNS_SECONDARY`, Docker Compose, `ss`, `curl`
- Produces: generated `.env`, `docker-compose.override.yml`, `Caddyfile`, and an accurate setup exit status

- [ ] **Step 1: Add project-scoped DNS defaults**

Add to the application service:

```yaml
dns:
  - "${CONTAINER_DNS_PRIMARY:-1.1.1.1}"
  - "${CONTAINER_DNS_SECONDARY:-8.8.8.8}"
```

Generate the same block for Caddy and write both variables to `.env`.

- [ ] **Step 2: Add non-destructive HTTPS port preflight**

Implement shell functions with these contracts:

```bash
project_caddy_running  # success only when card-issuance-caddy is running
port_in_use 80         # success when ss reports a TCP listener
show_port_owners       # prints ss and docker ps evidence
check_https_ports      # returns nonzero on third-party 80/443 conflicts
```

Run `check_https_ports` after HTTPS mode selection and before collecting secrets or building images. If `ss` is unavailable, print a warning and continue.

- [ ] **Step 3: Localize setup prompts and validation**

Translate all project-owned `setup.sh` prompts, validation failures, success output, and maintenance guidance into Chinese while preserving accepted input values such as `1`, `2`, `https`, and `http`.

- [ ] **Step 4: Wait for HTTPS readiness**

After Compose starts, poll the local Caddy listener using:

```bash
curl --silent --show-error --fail --head \
  --max-time 5 \
  --resolve "$domain:443:127.0.0.1" \
  "https://$domain/"
```

Use a bounded retry loop. On timeout, print recent Caddy logs, return nonzero, and retain the running containers for diagnosis.

- [ ] **Step 5: Run regression tests and verify GREEN**

Run:

```bash
bash tests/scripts/deployment_scripts_test.sh
bash -n scripts/*.sh tests/scripts/*.sh
```

Expected: PASS.

- [ ] **Step 6: Commit deployment behavior**

```bash
git add docker-compose.yml scripts/setup.sh tests/scripts/deployment_scripts_test.sh
git commit -m "fix: harden initial VPS deployment"
```

### Task 3: Chinese Maintenance Output and DNS Diagnostics

**Files:**
- Modify: `scripts/install.sh`
- Modify: `scripts/install-docker-ubuntu.sh`
- Modify: `scripts/deploy.sh`
- Modify: `scripts/update.sh`
- Modify: `scripts/diagnose.sh`
- Modify: `tests/scripts/deployment_scripts_test.sh`

**Interfaces:**
- Consumes: existing `.env`, generated Compose files, Docker inspection output
- Produces: Chinese operational messages and read-only container DNS evidence

- [ ] **Step 1: Extend tests for maintenance scripts**

Add assertions that script source contains the expected Chinese entry messages and that `diagnose.sh` inspects:

```bash
docker compose exec -T card-issuance cat /etc/resolv.conf
docker compose exec -T caddy cat /etc/resolv.conf
docker compose exec -T caddy nslookup acme-v02.api.letsencrypt.org
```

Run the suite and verify it fails before implementation.

- [ ] **Step 2: Translate project-owned output**

Translate prompts, validation errors, completion messages, and next-step guidance in all five maintenance scripts. Keep command names, paths, environment keys, and third-party output unchanged.

- [ ] **Step 3: Add read-only DNS diagnostics**

Add a `容器 DNS` section that prints `/etc/resolv.conf` from running project containers and attempts a Caddy-side lookup of the Let’s Encrypt API hostname. Every diagnostic command continues through failure using the existing `run` helper.

- [ ] **Step 4: Run tests and syntax checks**

Run:

```bash
bash tests/scripts/deployment_scripts_test.sh
bash -n scripts/*.sh tests/scripts/*.sh
```

Expected: PASS with no shell syntax errors.

- [ ] **Step 5: Commit maintenance improvements**

```bash
git add scripts tests/scripts/deployment_scripts_test.sh
git commit -m "chore: localize deployment diagnostics"
```

### Task 4: Deployment Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md` only if it describes Compose networking
- Test: `tests/scripts/deployment_scripts_test.sh`

**Interfaces:**
- Consumes: final script behavior and environment keys
- Produces: deployment instructions that match the shipped commands

- [ ] **Step 1: Document DNS defaults and override**

Add the two `.env` keys, explain that they affect only this Compose project, and document the recreate command:

```bash
docker compose up -d --force-recreate
```

- [ ] **Step 2: Document early port checks**

Explain that HTTPS mode requires free `80/443`, the installer reports existing listeners without stopping them, and users retaining V2bX/Nginx should choose HTTP direct mode and configure their existing reverse proxy.

- [ ] **Step 3: Run the complete verification suite**

Run:

```bash
bash tests/scripts/deployment_scripts_test.sh
bash -n scripts/*.sh tests/scripts/*.sh
git diff --check
git status --short
```

Also run the repository's Python checks:

```powershell
.\.venv\Scripts\python.exe -m compileall app
```

- [ ] **Step 4: Scan for secrets**

Run:

```bash
rg -n --no-ignore-vcs -P '(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|^(?:ADMIN_PASSWORD|GITHUB_TOKEN)=(?!\s*(?:(?i:(?:change[-_]?(?:this|me)|your)[A-Za-z0-9_-]*)|<[^>]+>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)\s*$).+)' . \
  --hidden -g '!data/**' -g '!backups/**' -g '!.venv/**' -g '!.git/**'
```

Expected: no matches.

- [ ] **Step 5: Commit documentation and push**

```bash
git add README.md docs
git commit -m "docs: explain reliable VPS deployment"
git push origin main
```
