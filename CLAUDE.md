# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Project Overview

Automation scripts for updating the Mozilla/NSS CA certificate trust list across Red Hat Enterprise Linux (RHEL), Fedora, and CentOS Stream releases. The pipeline downloads upstream `certdata.txt` from Mozilla NSS, applies RHEL-specific modifications, commits to dist-git, files Jira bugs, creates CRYPTO errata epics via cryptosvc, and triggers Brew/Koji builds.

## Pipeline: plan.py → build_combo.py → process.py

### plan.py — Planning step (run first)
Creates RHEL Jira bugs, requests z-stream clones, creates CRYPTO errata epics via cryptosvc, and triages bugs. Writes `meta/rhel.list` and `meta/fedora.list` so downstream scripts have pre-populated bug numbers.

```
./plan.py --rhel -f <firefox_version>    # auto-discover all active RHEL releases
./plan.py --fedora                        # auto-discover Fedora releases via Bodhi
./plan.py --help                          # full option reference
```

Key options: `-n <nss_version>`, `--dry-run`, `--loop`, `--human` (Rich TUI), `--dev-sprint`, `--qe-sprint`, `--crypto-epic-parent`.

### build_combo.py — Certdata update step
Downloads upstream certdata, applies per-release modifications, updates dist-git checkouts, and writes `staged` state back to the meta lists.

```
./build_combo.py                          # reads releases from meta/ (pipeline mode)
./build_combo.py rhel-10.3.0 rhel-9.9.0  # explicit releases (manual mode)
./build_combo.py --help                   # full option reference
```

### process.py — Build and errata lifecycle
Advances each release through: git checkin → push → (CentOS Stream MR + CI wait) → Brew/Koji build → Errata advisory.

```
./process.py
./process.py --loop --interval 300       # continuous mode
./process.py --human                     # Rich TUI
./process.py --help                      # full option reference
```

## Configuration

Copy `config.cfg.sample` to `config.cfg` and fill in credentials. `config.cfg` is gitignored.

Required keys: `owner`, `manager`, `qe`, `jira_url`, `jira_api_key`, `jira_user`, `centos_fork`, `glab_url_base`, `glab_api_key`, `se_contact`.

For cryptosvc integration (CRYPTO errata epics): `cryptosvc_url`, `cryptosvc_access_token`, `cryptosvc_pat`, `crypto_epic_parent`.

For staging/development: use `config.cfg.devel` (points at stage Errata/Jira, enables `dry_run:True`).

## Python Package: caupdate/

Shared utilities imported by all pipeline scripts:

| Module | Contents |
|--------|----------|
| `caupdate/release.py` | `release_get_major`, `safe_int`, `get_need_zstream_clone`, `discover_rhel_releases`, errata map loading, `_errata_is_better` |
| `caupdate/issues.py` | `JiraSession` (v3 API), `issue_create`, `issue_lookup`, `issue_request_clone`, `jira_fixversion`, `make_jira_client` |
| `caupdate/versions.py` | `fetch_nss_versions` — downloads nss.h/nssckbi.h from Mozilla |
| `caupdate/tui.py` | `PipelineOutput` — Rich TUI (status table + log) or plain timestamped stdout |
| `caupdate/release_config.py` | Per-major RHEL flags from `release_config.toml` |
| `caupdate/prereqs.py` | `check_prereqs` — verify required CLI tools and Kerberos ticket |

## Per-Major Release Configuration

`release_config.toml` controls per-major behaviour — **no version numbers in code**:

```toml
[8]
centos_stream          = false   # RHEL 8 ships via Brew directly
zstream_clone          = false
version_parts          = 3       # dist-git branches are X.Y.Z (e.g. rhel-8.10.0)
main_branch            = "rhel-8-main"
jira_keep_zero_below_minor = 10  # keep .0 in fixVersion for minor < 10

[8.releases."10"]
dist_branch = "rhel-8-main"     # 8.10 has no dedicated branch; use main directly

[9]
centos_stream          = true    # RHEL 9 GA goes through CentOS Stream c9s fork
zstream_clone          = true
version_parts          = 3       # dist-git branches are X.Y.Z (e.g. rhel-9.6.0)
centos_branch          = "c9s"

[10]
centos_stream          = true    # RHEL 10 GA goes through CentOS Stream c10s fork
zstream_clone          = true
# version_parts defaults to 2 → dist-git branches are X.Y (e.g. rhel-10.3)
centos_branch          = "c10s"
```

Add a new `[<major>]` section when a new RHEL major is introduced.

## Key Directories

- `meta/` — State files owned by plan.py: `rhel.list`, `fedora.list`, version txt files
- `packages/` — Cloned dist-git trees: `packages/{rhel,centos,centos-fork,fedora}/<stream>/`
- `modified/` — Per-release modified certdata output: `modified/{rhel8,rhel9,rhel10,fedora}/certdata.txt`
- `cacerts/` — Downloaded upstream `certdata.txt`, `nssckbi.h`, `nss.h`
- `tests/` — pytest test suite (285 tests)

## meta/rhel.list Format (10 fields)

`release:branch:bugnumber:erratanumber:nvr:state:glmr:glupstream:crypto_key:crypto_dev`

| Field | Example | Description |
|-------|---------|-------------|
| release | `rhel-9.6.0` | Release key |
| branch | `rhel-9.6.0` | dist-git branch / centos stream branch |
| bugnumber | `RHEL-212605` | Jira issue key |
| erratanumber | `171374` | Errata advisory number (0 = not found yet) |
| nvr | `ca-certificates-2026.2.90...` | Build NVR |
| state | `complete` | Pipeline state (see below) |
| glmr | `52` | GitLab MR IID |
| glupstream | `23656918` | Upstream project ID |
| crypto_key | `CRYPTO-23470` | CRYPTO QE/errata epic key |
| crypto_dev | `CRYPTO-23553` | CRYPTO DEV child issue key |

States: `planned` → `staged` → `committed` → `pushed` → `builds in progress` → `builds complete` → `errata ready QE` → `complete`

CentOS Stream states: `waiting centos ci` → `centos ci failed` (terminal) → `waiting centos merge`

## Jira / Cloud API Notes

- RHEL Jira is at `redhat.atlassian.net` (Jira Cloud) — uses Basic Auth (`email:api_token`), NOT Bearer
- `issues.redhat.com` redirects to `redhat.atlassian.net` — auto-resolved at client init
- All search/create uses v3 API endpoints (`/rest/api/3/`) — v2 was removed from Cloud in 2026
- `jira_fixversion()`: strips trailing `.0` for RHEL 9.4+ and 10+; RHEL 8 (minor<10) and RHEL 9 (minor≤2) keep the `.0` (e.g. `rhel-8.4.0.z`, `rhel-9.2.0.z`). Driven by `version_parts` and `jira_keep_zero_below_minor` in TOML.
- `customfield_10879` = "Preliminary Testing"; option id `20445` = "Requested" — set when gating passes

## Sustaining Engineering

Releases with E4S, E2S, AUS, or TUS in the errata product name are sustaining engineering:
- `se_contact` (from config.cfg) is set as QA contact instead of `qe`
- No CRYPTO epic is created; only the RHEL bug is filed
- `is_sustaining_release(pv_name)` in `caupdate/release_config.py` identifies these

## cryptosvc Integration

Calls the running cryptosvc at `https://sec-crypto.users.ipa.redhat.com/cryptosvc/`:
- `POST /jira/errata/create` — creates CRYPTO errata Epic + placeholder tasks
- `POST /jira/triage` (two calls) — sets priority/severity/regression, then `{"status":"Planning"}` to create [DEV]/[QE] CRYPTO splits
- Requires: Kerberos ticket + `Access-Token` + `PAT` headers

## Errata Lifecycle

- Errata are now auto-created by the Errata Tool; `errata_create` in process.py is disabled (kept for reference)
- `errata_get_bugs` reads from `jira_issues.jira_issues[].jira_issue.key` (post Jira migration)
- Completion trigger: rpm diff state PASSED/WAIVED/INFO → set Preliminary Testing Requested → close CRYPTO DEV → `complete`
- `_ga_release_id` uses `params=` for correct URL encoding of names containing `+` (e.g. MAIN+EUS)

## Build Tooling

RHEL builds use `rhpkg` (Brew), Fedora uses `fedpkg` (Koji), CentOS Stream uses `centpkg`. RHEL 8 goes directly to Brew; RHEL 9+ GA goes through CentOS Stream fork first.

## Testing

```
pytest tests/ -v                         # all 285 tests
pytest tests/test_caupdate_release.py    # caupdate/release.py unit tests
pytest tests/test_caupdate_issues.py     # caupdate/issues.py unit tests
pytest tests/test_build_combo_py.py      # build_combo.py Python tests
```

## One-off Fix Scripts

- `fix_crypto_parent.py` — sets `crypto_epic_parent` on all existing CRYPTO epics
- `fix_se_contacts.py` — retroactively triages RHEL bugs and sets QA contacts

## Required External Tools

`rhpkg`, `fedpkg`, `centpkg`, `git`, `brew`, `koji`, `kinit` (Kerberos). All pipeline scripts call `check_prereqs()` at startup and report missing tools clearly.
