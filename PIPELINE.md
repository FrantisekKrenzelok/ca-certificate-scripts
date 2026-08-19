# CA Certificates Update Pipeline

Complete developer reference for the `plan.py → build_combo.py → process.py` pipeline.

---

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     CA Certificates Annual Update                            │
└─────────────────────────────────────────────────────────────────────────────┘

  PRE-FLIGHT                    STEP 1                   STEP 2
  ─────────                    ──────                   ──────
  config.cfg          ──────►  plan.py            ───► build_combo.py
  • credentials                • discover releases       • fetch certdata from
  • nss:126                    • create RHEL Jira bugs     Mozilla NSS tag
  • firefox version            • create CRYPTO epics     • merge codesign CAs
  • centos fork URL            • write meta/rhel.list    • modify per-RHEL
                               • write meta/fedora.list  • update dist-git
                                                           worktrees (staged)
                                                         • update tests repo
                                                         • write meta/*version


  STEP 3 (loop until all complete)
  ──────
  process.py
  ┌──────────────────────────────────────────────────────────┐
  │  Pass 1 — CentOS-stream GA releases (rhel-9.9, rhel-10.3)
  │    git add → commit → push to centos-fork               │
  │    create GitLab MR → wait CI → automerge               │
  │    rhpkg build (Brew)                                    │
  │                                                          │
  │    ── gate: all centos builds must settle ──             │
  │                                                          │
  │  Pass 2 — RHEL z-stream releases (rhel-9.6, rhel-8.10…) │
  │    git add → commit → push to dist-git branch           │
  │    rhpkg/brew build                                      │
  │    errata lookup → attach bug & build                   │
  │    rpm diff PASSED → Preliminary Testing Requested       │
  │    → close CRYPTO DEV → complete                        │
  └──────────────────────────────────────────────────────────┘
```

---

## Before You Start

### 1. config.cfg

Copy `config.cfg.sample` → `config.cfg` and fill in:

| Key | What it is | Used by |
|-----|-----------|---------|
| `owner` | Package owner email | plan.py (Jira fields) |
| `manager` | Manager email | plan.py (Jira fields) |
| `qe` | QE contact email | plan.py, process.py |
| `se_contact` | Sustaining Engineering contact email | plan.py, process.py |
| `jira_url` | `https://issues.redhat.com` | plan.py, process.py |
| `jira_user` | Your RH email (Jira Cloud login) | plan.py, process.py |
| `jira_api_key` | Jira Cloud personal access token | plan.py, process.py |
| `centos_fork` | SSH URL of your GitLab fork | build_combo.py, process.py |
| `glab_url_base` | `https://gitlab.com/` | build_combo.py, process.py |
| `glab_api_key` | GitLab personal access token | build_combo.py, process.py |
| `cryptosvc_url` | `https://sec-crypto.users.ipa.redhat.com/cryptosvc` | plan.py |
| `cryptosvc_access_token` | From cryptosvc admin | plan.py |
| `cryptosvc_pat` | From browser DevTools after visiting cryptosvc | plan.py |
| `crypto_epic_parent` | CRYPTO-XXXXX parent epic key | plan.py |
| `nss` | NSS minor version, e.g. `126` (→ 3.126) | build_combo.py |
| `firefox` | Firefox version for bug text | plan.py, process.py |
| `version` | CKBI version override (optional) | plan.py |

**Staging/dev:** use `config.cfg.devel` (points at stage Errata/Jira, sets `dry_run:True`).

### 2. Prerequisites

```bash
kinit                  # valid Kerberos ticket required
rhpkg --version        # dist-git RHEL tool
centpkg --version      # dist-git CentOS Stream tool
fedpkg --version       # dist-git Fedora tool (for Fedora runs)
brew --version         # Brew build system
koji --version         # Koji build system (Fedora)
```

---

## Step 1 — plan.py

**Discovers releases, creates Jira bugs, creates CRYPTO epics, writes meta files.**

```bash
# RHEL mode — auto-discover all active RHEL releases
./plan.py --rhel -f <firefox_version>

# Fedora mode
./plan.py --fedora

# Both
./plan.py --rhel --fedora -f <firefox_version>
```

### Key options

| Flag | Description |
|------|-------------|
| `-f <ver>` | Firefox version string (required for bug text) |
| `-n <ver>` | Override NSS version for bug text |
| `--dry-run` | No Jira/cryptosvc writes |
| `--resync` | Force refresh of errata cache |
| `--human` | Rich TUI output |
| `--crypto-epic-parent <KEY>` | Override CRYPTO parent epic |
| `--dev-sprint <id>` | Set dev sprint on CRYPTO sub-issues |
| `--qe-sprint <id>` | Set QE sprint on CRYPTO sub-issues |

### What plan.py does

1. Discovers active RHEL releases via errata product versions
2. For each release: looks up existing Jira bug → creates if missing
3. Creates CRYPTO errata epic via cryptosvc (skipped for SE/sustaining releases — E4S, E2S, AUS, TUS)
4. Requests z-stream clones for GA bugs (RHEL 9+)
5. Writes `meta/rhel.list` and `meta/fedora.list`

### After plan.py

```
meta/rhel.list    — one line per release, state=planned
meta/fedora.list  — one line per fedora release, state=planned
```

**rhel.list format** (10 fields):
```
release:branch:bugnumber:erratanumber:nvr:state:glmr:glupstream:crypto_key:crypto_dev
```

---

## Step 2 — build_combo.py

**Downloads certdata, applies RHEL modifications, updates dist-git checkouts.**

```bash
# Pipeline mode — reads releases from meta/rhel.list
./build_combo.py

# Manual mode — specific releases only
./build_combo.py rhel-10.3.0 rhel-9.9.0 rhel-9.6.0

# Common options
./build_combo.py --human          # Rich TUI
./build_combo.py -n 3.126         # Override NSS version (or set nss:126 in config.cfg)
./build_combo.py -d               # Use NSS dev branch
./build_combo.py -f /path/to/dir  # Use local certdata dir instead of downloading
```

### Key options

| Flag | Description |
|------|-------------|
| `-n <ver>` | NSS version (overrides `nss` in config.cfg) |
| `-t <type>` | NSS release type: RTM, BETA1, BETA2 (default: RTM) |
| `-f <dir>` | Use local certdata directory |
| `-p <date>` | Prune date for old certs |
| `--human` | Rich TUI |
| `releases...` | Explicit releases (bypasses meta/rhel.list) |

### What build_combo.py does

1. Reads releases from `meta/rhel.list` (pipeline mode) or CLI args
2. Downloads `certdata.txt`, `nss.h`, `nssckbi.h` from Mozilla NSS at the tag
3. Fetches Microsoft codesigning CAs, merges into certdata
4. Clones / updates dist-git repos:
   - `packages/rhel/ca-certificates/` — main rhel repo (worktrees per z-stream branch)
   - `packages/centos-fork/c9s/`, `packages/centos-fork/c10s/` — your GitLab fork (GA releases)
   - `packages/fedora/` — fedora dist-git
5. Resolves centos-fork upstream URL via GitLab API (`fork.forked_from_project`)
6. Updates spec file (Version, Release, %changelog) and copies certdata.txt in each worktree
7. Sets state to `staged` in meta lists

### Dist-git branch naming

Controlled by `version_parts` in `release_config.toml`:

| Major | version_parts | Branch format | Example |
|-------|--------------|--------------|---------|
| RHEL 8 | 3 (explicit) | `rhel-X.Y.Z` | `rhel-8.10.0` |
| RHEL 9 | 3 (explicit) | `rhel-X.Y.Z` | `rhel-9.6.0` |
| RHEL 10+ | 2 (default) | `rhel-X.Y` | `rhel-10.2` |
| GA releases | — | `rhel-<major>-main` via centos-fork | `c9s`, `c10s` |

### After build_combo.py

```
packages/rhel/<branch>/          — staged changes ready to commit
packages/centos-fork/c9s/        — staged changes (GA rhel-9.9)
packages/centos-fork/c10s/       — staged changes (GA rhel-10.3)
meta/rhel.list                   — state=staged for all releases
meta/nssversion.txt              — actual NSS version downloaded
meta/ckbiversion.txt             — CKBI version
```

---

## Step 3 — process.py

**Advances each release through git checkin → build → errata lifecycle.**

```bash
./process.py                          # single pass
./process.py --loop --interval 300    # loop every 5 min until all complete
./process.py --human                  # Rich TUI status table
./process.py --dry-run                # no builds or errata writes
```

### Key options

| Flag | Description |
|------|-------------|
| `--loop` | Re-exec after each pass until all releases are terminal |
| `--interval <sec>` | Loop sleep interval (default 300s) |
| `--human` | Rich TUI with status table and live countdown |
| `--dry-run` | Read-only: no commits, builds, or Jira/errata updates |
| `--resync` | Force errata cache refresh |

Press `Ctrl+C` or `Ctrl+D` during the loop countdown to exit cleanly.

### Processing order

```
Pass 1 — CentOS-stream GA releases
    For each (rhel-9.9.0, rhel-10.3.0):
      staged → commit → push to centos-fork
      create/check GitLab MR → wait CI → merged
      rhpkg build → Brew

    ── GATE: wait until all centos releases are 'complete' or 'centos ci failed' ──

Pass 2 — RHEL z-stream releases
    For each (rhel-9.6.0, rhel-8.10.0, rhel-10.2 …):
      staged → commit → push to dist-git branch
      rhpkg build → Brew
      set QA contact (se_contact for E4S/E2S/AUS/TUS, qe otherwise)
      update Jira bug summary/description with real versions
      errata lookup → attach bug → attach builds
      rpm diff PASSED/WAIVED/INFO:
        → set Preliminary Testing = Requested (customfield_10879)
        → close CRYPTO DEV child issue
        → state = complete
```

### State machine

```
planned ──► staged ──► committed ──► pushed ──► builds in progress
                                                      │
                            ┌─── centos stream ───────┤
                            │    waiting centos ci     │
                            │    waiting centos merge  │
                            │    centos ci failed ──►(terminal)
                            └─────────────────────────┘
                                                      │
                                               builds complete
                                                      │
                                              errata found/attached
                                                      │
                                             errata ready QE
                                                      │
                                           rpm diff PASSED/WAIVED/INFO
                                                      │
                                              ► complete (terminal)
```

---

## meta/ Files

| File | Written by | Content |
|------|-----------|---------|
| `rhel.list` | plan.py, build_combo.py, process.py | Release pipeline state (10 fields) |
| `fedora.list` | plan.py, build_combo.py, process.py | Fedora release state (5 fields) |
| `nssversion.txt` | build_combo.py | e.g. `3.126` |
| `ckbiversion.txt` | build_combo.py | e.g. `2.90_v9.0.316` |
| `mcsversion.txt` | build_combo.py | e.g. `v9.0.316` |
| `firefox_info.txt` | plan.py | Firefox version string |

**rhel.list** annotated example:
```
rhel-9.6.0:rhel-9.6.0:RHEL-212605:171374:ca-certificates-2026...:complete:::CRYPTO-23470:CRYPTO-23553
│           │           │           │       │                       │     │  │  │            │
│           branch      bugnumber   │       nvr                     state │  │  crypto_key  crypto_dev
│                                   erratanumber                          │  glupstream
release                                                                   glmr
```

---

## Configuration in release_config.toml

Per-major version rules — **no version numbers in code**:

```toml
[9]
centos_stream          = true   # goes through CentOS Stream fork
zstream_clone          = true   # GA bug is cloned for all active z-streams
version_parts          = 3      # branch names use X.Y.Z
centos_branch          = "c9s"
main_branch            = "rhel-9-main"
restart_release_z      = "90.0"
restart_release_base   = "91"
jira_keep_zero_below_minor = 3  # rhel-9.0–9.2 keep .0 in fixVersion

[10]
centos_stream          = true
zstream_clone          = true
# version_parts defaults to 2 → branch names use X.Y (e.g. rhel-10.2)
centos_branch          = "c10s"
main_branch            = "rhel-10-main"

[8]
centos_stream          = false  # RHEL 8 goes direct to Brew
zstream_clone          = false
version_parts          = 3
main_branch            = "rhel-8-main"
jira_keep_zero_below_minor = 10

[8.releases."10"]
dist_branch = "rhel-8-main"     # rhel-8.10 has no dedicated branch
```

---

## Quick Reference: Full Cycle

```bash
# 1. Setup
cp config.cfg.sample config.cfg
# edit config.cfg — fill in all credentials + nss:126

# 2. Plan (creates Jira bugs + CRYPTO epics)
kinit
./plan.py --rhel -f "138.0"

# 3. Build (downloads certdata, stages packages)
./build_combo.py --human

# 4. Process (commit → build → errata, loops until done)
./process.py --loop --human
```

Press `Ctrl+C` or `Ctrl+D` during the loop sleep to exit cleanly.
