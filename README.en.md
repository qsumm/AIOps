<div align="center">

# TestOPS · AI-Powered Ops Monitoring Platform

**The alert fired. The AI already finished investigating. Here is the evidence chain.**

An open-source, self-hosted AI ops monitoring platform: a pull-based Agent takes over host and service monitoring (**zero inbound ports** on monitored machines), alerts are converged into incidents, and a **diagnosis Agent with an evidence chain** investigates autonomously; remediation runs only from a fixed action allowlist and only after passing nine guardrails — **the AI establishes the facts, the human keeps the decision**.

[English](README.en.md) | [简体中文](README.md)

[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](deploy/docker-compose.yaml)
[![Release](https://img.shields.io/badge/release-v1.8.1-success.svg)]()

![The complete loop of a real incident](docs/images/hero-loop.svg)

| **82 s** | **10 min** | **0** |
|:---:|:---:|:---:|
| alert refresh (3 probe types) | evidence-backed root cause report | inbound ports (pull-based Agent) |

*All numbers above come from a real incident run, including the CONFIRMED verdict and the full audit trail.*

```bash
git clone https://github.com/qsumm/TestOPS.git && cd TestOPS
cp backend/.env.example .env   # replace JWT_SECRET / KEY_STORE_SECRET / DB_PASSWORD with random values!
docker compose --env-file .env -f deploy/docker-compose.yaml up -d --build
```
> First build takes about 3–6 minutes (pip via a China mirror; swap sources if you prefer upstream). If GitHub is slow, use a clone accelerator — see [Deployment](#deployment).

</div>

---

## Why this exists

Ever had that night: an alert fires, you SSH in, dig through logs, inspect processes, guess the cause — and half an hour later you realize you restarted a service that was never broken.

This platform hands that whole procedure to an AI — but **not** by letting the AI loose on your production box. The philosophy is written into the code:

> The hard part of remediation is not "executing", it is "**daring not to execute**".

## Core features

![Overview dashboard](docs/screenshots/tutorials/02-dashboard.png)

**Monitoring** — pull-based Agent onboarding (**zero inbound ports** on monitored hosts, no firewall / NAT / security-group changes) · four probe types (process / TCP / HTTP / certificate expiry) · multi-dimensional metric queries on demand · Prometheus data source · host and service health overview

**Alerting** — threshold / 3σ / isolation-forest algorithms · fingerprint deduplication (one fault becomes one incident, no alert storms) · silencing chains and maintenance windows · multi-channel notification (email / Webhook / **voice call broadcast**) · an immediate directional hint on every trigger

**AI diagnosis** — multi-step ReAct investigation (what to check next is decided by the previous result, every step recorded and replayable frame by frame) · **conclusions must cite evidence** (each claim links back to the step that produced it) · three confidence levels (**"could not find out" is a legitimate conclusion**) · explicit `not_checked` section · pluggable diagnosis skills (SKILL.md plugins, auto-loaded by alert, only pre-authorized read-only commands are executed)

**Remediation** — an allowlist of just 4 actions (restart service / restart container / reconnect pool / rotate logs) · nine guardrails for admission (read-only classification → denylist → allowlist → rate limit & circuit breaker → release-window mutual exclusion → refuse self-healing when upstream is unhealthy → human approval) · three execution modes (suggest only / manual confirm / allowlist auto) · **mandatory post-check** after execution, failures trip the circuit breaker · versioned change requests and a full operation audit trail

**Accumulation & evaluation** — closing an incident automatically distills a vectorized case card, so the next similar failure recalls "how we fixed it last time" · scheduled inspections produce daily health reports and open incidents for anomalies · a diagnosis evaluation loop: fault recording → replay, root-cause agreement / conflict / calibration scoring, version fingerprinting

**Extensibility** — full REST API + MCP integration, embeddable in your own toolchain

## Six core design decisions

| | Design | One line |
|---|---|---|
| <img src="docs/images/icons/search.svg" width="20" align="top"/> | **AI diagnosis with an evidence chain** | Up to 30 steps, every thought / tool call / return recorded; conclusions must cite evidence — "could not find out" is a legitimate conclusion |
| <img src="docs/images/icons/shield.svg" width="20" align="top"/> | **Nine guardrails decide admission** | Action allowlist (no free-text commands), rate limits, circuit breaker, release-window exclusion, refuse self-healing when upstream is unhealthy… fail any one and nothing may move |
| <img src="docs/images/icons/route.svg" width="20" align="top"/> | **Pull-based channel punches through NAT** | The Agent **pulls outbound** from the host, which opens no inbound port — works behind firewalls, NAT and cloud security groups with zero changes |
| <img src="docs/images/icons/layers.svg" width="20" align="top"/> | **Diagnosis skills are pluggable** | A skill is just a SKILL.md file — auto-loaded when an alert matches, and the Agent may only run the read-only commands the skill pre-authorizes; one-click import from GitHub, with hit and adoption counts |
| <img src="docs/images/icons/database.svg" width="20" align="top"/> | **The case library gets smarter with use** | Every diagnosis is distilled into a vectorized case, so the next similar failure recalls "how we fixed it last time" |
| <img src="docs/images/icons/zap.svg" width="20" align="top"/> | **5-minute deployment** | One compose command; vector search is built into the database — no extra Milvus / Pinecone |

## What it does / does not do

**Does**: host and service monitoring, proactive alerting (multi-channel notification + voice broadcast), AI root-cause diagnosis (with an evidence chain), automatic skill routing for diagnosis (read-only authorized commands), allowlisted remediation actions (restart / reconnect / rotate logs), scheduled inspections, multi-dimensional metric queries, case accumulation.

**Does not**: replace your Prometheus (it sits behind it), unattended autonomy (the default execution mode requires human confirmation), or run any command outside the allowlist (an AI-proposed `rm -rf` is blocked at the very first guardrail).

## What a diagnosis report looks like

Every conclusion carries a step reference you can click back to the raw evidence, and anything it could not determine is listed explicitly in `not_checked` — **"could not find out" is a legitimate conclusion**:

![A CONFIRMED diagnosis report](docs/images/diag-report-confirmed.png)

## Architecture at a glance

![System architecture](docs/images/architecture.svg)

**The diagnosis loop** (every step recorded end to end, replayable):

![Diagnosis loop diagram](docs/images/diag-loop.svg)

## Deployment

### Prerequisites

| Item | Requirement | Notes |
|---|---|---|
| Docker + Compose | Docker 20.10+, `docker compose` v2 | `docker compose version` should print a version |
| Memory | 4GB recommended | Platform + database + two simulated hosts run together; 2GB gets OOM-killed |
| Disk | 10GB recommended | 820MB database image plus dependency layers |
| Ports | 8848 (platform), 2891 (database) | Change the `ports` mapping in `deploy/docker-compose.yaml` if taken |
| Network | The first build needs access to the image registry and pypi | China-friendly mirrors are the default; no VPN needed |

### Deploy in three steps

```bash
git clone https://github.com/qsumm/TestOPS.git && cd TestOPS
cp backend/.env.example .env   # ① copy the env template
docker compose --env-file .env -f deploy/docker-compose.yaml up -d --build   # ② build and start
```

③ **Generate and fill in three secrets** (replace `change-me-in-env` and the empty `DB_PASSWORD` in `.env`):

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # run three times: JWT_SECRET / KEY_STORE_SECRET / DB_PASSWORD
```

- `JWT_SECRET` / `KEY_STORE_SECRET`: **must be changed for any public deployment**. The first signs login tokens; the second encrypts the LLM API keys you enter in the settings page before they hit the database — shipping the defaults is like hanging the key on the door
- `DB_PASSWORD`: may stay empty for a local trial; required for public or multi-user use

After editing `.env`, run `docker compose --env-file .env -f deploy/docker-compose.yaml up -d` to apply.

### Verify it works

```bash
curl http://127.0.0.1:8848/api/health      # expect {"code":0,...,"status":"up"}
docker ps                                   # all 4 containers should be Up / healthy
docker logs aiops-platform | grep -i "admin"   # the generated admin account is printed here on first start
```

Open `http://127.0.0.1:8848` and log in with the account printed in the logs (**change the password on first login**). The platform is running at this point, but **AI features still have no model** — go to "Settings → LLM Configuration" and fill in an OpenAI-compatible endpoint (Base URL / API Key / model name). Monitoring, alerting, probing and inspection work without a model.

### See it work right away

```bash
# seed 7 days of demo data (metrics + rules + historical incidents + case cards)
docker compose --env-file .env -f deploy/docker-compose.yaml exec platform \
    python scripts/seed_demo.py --days 7

# trigger a real fault (5 kinds: probe failure / CPU / disk / memory / process hang — one command each, auto recovery)
bash deploy/faults/cpu_spike.sh
```

### Common issues

| Symptom | Cause and fix |
|---|---|
| `git clone` times out / `end of file` | GitHub access from mainland China is flaky; use an accelerator: `git clone https://gh-proxy.com/https://github.com/qsumm/TestOPS.git` |
| Build hangs at `apt-get` | Base image and pip already default to China mirrors; if it still hangs, switch `APT_MIRROR` / `PIP_INDEX_URL` in the Dockerfile back to the upstream sources |
| Container keeps restarting (`Restarting`) | Out of memory (4GB+ required), or the database is still initializing; check `docker logs aiops-platform` |
| Login says "invalid install token" | First-boot Agents need a one-time install token: generate one under "Hosts → Onboard a new host", put it in `.env` as `AIOPS_INSTALL_TOKEN_1/_2`, then `docker compose ... up -d` to recreate the agent containers |
| AI diagnosis says "model not configured" | "Settings → LLM Configuration" is empty or disabled; fill it in, then trigger a diagnosis |
| Port already in use | Change the `8848` / `2891` port mappings in `deploy/docker-compose.yaml` |

### Data and teardown

```bash
docker compose --env-file .env -f deploy/docker-compose.yaml down      # stop services, keep data
docker compose --env-file .env -f deploy/docker-compose.yaml down -v   # also delete the data volume (irreversible)
```

Data lives in the Docker volume `aiops-demo_seekdb-data` (time-series metrics + incidents + cases); backing up means exporting that volume.

> The full operation manual is not in this repository: follow the WeChat account「一鸣AI编程」and share the article to get it.

## Comparison with open-source peers

| # | Capability | **TestOPS (this project)** | Nightingale | Keep | k8sgpt | HolmesGPT |
|---|---|---|---|---|---|---|
| 1 | Unified Agent collection (metrics + probes + resumable upload) | ✅ pull-based, 0 inbound ports | ✅ Categraf (push) | ❌ | ❌ | ❌ |
| 2 | Alert engine (fingerprint dedup + silencing chains + active windows) | ✅ | ✅ strong point | ◐ alert aggregation | ❌ | ❌ |
| 3 | Tiered notification + voice broadcast + routing | ✅ incl. tested voice calls | ◐ many channels | ✅ 100+ integrations | ❌ | ❌ |
| 4 | Two-level convergence into incidents (LLM causal merging) | ✅ | ◐ suppression / distribution | ✅ strong AI alert correlation | ❌ | ◐ single-alert analysis |
| 5 | AI root-cause diagnosis (ReAct evidence chain + skill routing) | ✅ | ◐ AI summaries (verified in source), RCA via ecosystem | ◐ AI enrichment / summaries | ✅ K8s objects only | ✅ strong point |
| 6 | Fixes validated before being recommended | ✅ | ❌ | ◐ workflows can self-validate | ◐ suggestions, not enforced | ◐ iterative, not normative |
| 7 | Allowlisted remediation + nine guardrails + three execution modes | ✅ one-time approval | ◐ self-heal scripts, no admission layer | ◐ workflow automation, no guardrails | ◐ K8s-layer auto repair | ❌ |
| 8 | Vector recall of past cases | ✅ built-in vector search | ❌ | ❌ | ❌ | ◐ history in commercial edition |
| 9 | Scheduled inspection + health digest + auto incident creation | ✅ | ❌ | ◐ assemble via workflows | ❌ | ❌ |
| 10 | Service-level monitoring + log forensics (time-window anchored to the alert) | ✅ | ◐ log data sources | ❌ | ❌ | ◐ can query log sources |
| 11 | Deep in-cluster K8s support (operator / auto repair / custom analyzers) | ❌ | ◐ cloud-native integration | ◐ | ✅ | ✅ |
| 12 | Declarative automation workflows (YAML) | ◐ 4 allowlisted actions + heal policies, no general orchestration | ◐ scripted self-healing | ✅ strong point | ◐ | ◐ structured runbooks |
| 13 | Full incident-management lifecycle (on-call rota / escalation / ticketing) | ❌ | ◐ | ✅ strong point | ❌ | ❌ |

## Security

- LLM API keys are stored **AES-GCM encrypted**; the API only ever returns the last few characters
- Remediation actions are an **enumerated allowlist** (4 of them), and each action declares a mandatory post-check
- All API endpoints were self-tested with a five-vector attack matrix (unauthenticated / privilege escalation / path mutation / parameter mutation / body mutation); the tool is open-sourced in `tools/attack_matrix.py`
- Report vulnerabilities privately via GitHub Security Advisories

## 🤝 Contributing

Issues and pull requests are welcome. What this project needs most is not "a feature request" but **"fix the bug you hit on a real machine"** — every real failure, every false alarm, every guardrail that misfired is the most valuable contribution.

**Issues**: for bugs, include "environment + reproduction steps + expected vs actual + relevant logs" (platform logs via `docker logs aiops-platform`; Agent logs come from the agent output on the target machine). Search before opening a feature request.

**Pull requests**:

```bash
git clone https://github.com/qsumm/TestOPS.git && cd TestOPS
# dev environment: FastAPI backend with SQLite or SeekDB, build-free frontend
./.venv/bin/python -m pytest backend/tests -q     # the full suite must be green
```

A few hard rules:

1. **The frontend is build-free**: `static/js/pages/*.js` is loaded as native ES modules — edit, refresh, done. Do not introduce a bundler
2. **Frontend changes must pass the syntax gate**: `./.venv/bin/python -m pytest backend/tests/test_d3_frontend_assets.py -q` (one missing bracket breaks the whole module graph and the page goes blank with no error)
3. **Backend changes need tests**: name them `test_d<number>_<topic>.py` under `backend/tests/`
4. **Never commit real assets**: real IPs, real hostnames, API keys, alert screenshots and internal documents stay out of the repo; demo data always uses `demo-*` names
5. **Run the sensitive-data scan before committing**: `python tools/oss_export.py` refuses to export a package that trips the sensitive-word check — run it locally first

**Security vulnerabilities** must not be opened as public issues; report them privately through GitHub Security Advisories.

**Everything else**: feature ideas, documentation improvements, translations and sample datasets are all welcome — a short comment on an issue first saves a lot of wasted PRs.

## License

[AGPL-3.0](LICENSE).

---

<div align="center">

## Author

**Yiming** · WeChat official account「**一鸣AI编程**」

*The full journey of this platform — from the first line of code to the AI diagnosis engine — every design trade-off and every pitfall, serialized in the account.*

**WeChat account「一鸣AI编程」**: production incident post-mortems, hands-on AI coding, and the making of this platform.

If it helps you, a **Star ⭐** is the best support.

</div>
