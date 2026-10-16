# Issue policy: classifying, queueing, releasing and closing issues

Status: **policy, accepted by the owner on 2026-10-16** (merged in #145), after review by the Direct Connect bridge session.

**Applies to both bridges:** `franklinwh-modbus-bridge` and `franklinwh-direct-connect-bridge`.
Shared policy documents live in **franklinwh-modbus-bridge `docs/`**, and the Direct Connect repo
links to them.

**Source.** This builds on the owner's **Change Management Policy** in the `franklinwh-modbus`
library repo, `.agents/policies/change_management.md` (effective 2026-03-18). That policy's
severities, its queue-first rule and its batch rule apply unchanged. This document adds what the
bridges need on GitHub: labels, milestones, and when an issue may close. The library repo keeps its
own copy of the policy.

| Document | Says |
|---|---|
| This document | what an issue is, how bad it is, when it ships, and when it's finished |
| `docs/install-lifecycle.md` | what install, update and uninstall must do to HA and MQTT |
| `docs/TEST_POLICY.md` (shared; gates G0–G3, §2) | what a change must pass before it ships |

## 1. The rules carried over from the Change Management Policy

- **Queue first.** A reported defect or request is queued, never acted on the moment it's
  reported, unless it is **S1**. Asked to fix a non-S1 "now", the agent says it is queued and asks
  whether it is an S1.
- **Group, plan, execute, verify.** Queued items are grouped by area. A batch gets a written plan
  before any code changes, and one test and verification run at the end.
- **One version bump per batch, not per fix.**
- **No untested fixes,** and every change states its regression risk.
- **No cascading fixes.** A fix that causes a new defect stops; the new defect is queued.

## 2. Severity: how bad it is

The Change Management Policy's definitions (§1.2), with hardware safety added to S1 from its §3 exceptions:

| Label | Severity | Definition | Response |
|---|---|---|---|
| `sev:S1-blocker` | S1 Blocker | System down, data loss, security vulnerability, hardware safety, cannot proceed | Fix now, with a brief plan first |
| `sev:S2-high` | S2 High | Feature broken, but a workaround exists | Queue → next planned batch |
| `sev:S3-medium` | S3 Medium | Cosmetic, UX improvement, non-functional | Queue → group by area |
| `sev:S4-low` | S4 Low | Nice to have, future feature, clean-up | Queue → backlog review |

Points the bridges have to settle explicitly:
- **Broken with no workaround** is S2 at least. It is **S1** when it involves wrong battery control,
  a write to the device, security or data, or when nobody can use the feature at all.
- **A regression** (the previous release didn't have it) is raised one level, so a regressed S2 is
  an S1.
- **Features get a severity too** (S3 for UX improvements, S4 for future features), as in the
  source policy.
- When unsure between two levels, take the **higher** one and say why in the issue.

## 3. Labels

Every issue gets these labels **when it's filed**, by whoever files it: owner, contributor or agent.

| Group | Labels | Notes |
|---|---|---|
| Type | `bug`, `enhancement`, `documentation`, `chore` | GitHub's defaults kept; `chore` added (dependencies, CI, tooling, refactors with no behaviour change) |
| Severity | `sev:S1-blocker` … `sev:S4-low` | §2 |
| Area | see below | one per release area |
| Nature (optional) | `nature:code`, `nature:design-risk`, `nature:device`, `nature:doc`, `nature:test-gap` | from the Direct Connect bridge; `nature:device` marks aGate deviations, `nature:test-gap` a gate that missed it |
| State | `untriaged`, `needs-info`, `blocked`, `fixed`, `wontfix`, `duplicate`, `regression` | §5 |

**Area labels** name the test policy's release areas (TEST_POLICY §1), so "one area per release" can
be read off the labels:

`area:store-migrations` · `area:poller` · `area:mqtt-ha-identity` · `area:mqtt-ha-lifecycle` ·
`area:wizard-ui` · `area:http-api` · `area:modbus-client` · `area:scheduler-dispatch` ·
`area:packaging` · `area:client-transport` (Direct Connect)

- Documentation-only work uses `documentation` with no area label; it isn't a release area.
- **Which Modbus:** `area:modbus-client` is the bridge's own Modbus client code. The aGate's
  Modbus TCP server (:502) is a device setting, not our code; problems with it are
  `nature:device`.

## 4. When it ships: milestones

The *response* column in §2 decides when an issue is worked on. A **milestone** records it:

| Severity | Milestone |
|---|---|
| S1 | a hotfix release milestone, now |
| S2 | the next planned batch for its area |
| S3 | a batch for its area, when one is planned |
| S4 | none; it stays queued until the backlog review (§7) picks it up |

- **One batch = one area = one release = one milestone** (`0.2.8`, `0.3.0`, …). The batch rule in §1
  and the test policy's one-area-per-release rule are the same thing.
- The order of batches follows `docs/install-lifecycle.md` §8.
- **A batch has one fix PR or several, and one release PR** (version bump plus CHANGELOG only).
- **No release on the same day as the previous one,** except an S1 hotfix (test policy).

### Queue files and issues
- **franklinwh-modbus-bridge:** GitHub issues are the queue. Every defect gets an issue when it's
  found, including during other work. Don't fix things silently.
- **franklinwh-direct-connect-bridge:** `BACKLOG.md` stays the design record (why and how).
  `docs/IN_FLIGHT.md` tracks work underway. An item gets an issue when it is picked into a batch, and
  defects always get one when found. Its CLAUDE.md is updated to point here.

## 5. Lifecycle of an issue

```
new (untriaged) → triaged → planned → in progress → fixed → verified → closed
       └──→ needs-info / blocked             └──→ wontfix / duplicate
```

| State | How to find it | Who moves it on |
|---|---|---|
| **untriaged** | label `untriaged`, or no `sev:` label | the filer labels it at filing (§3) |
| **triaged** | has type, `sev:` and `area:` | the filer. The owner may change it; if the owner is silent, the filer's labels stand |
| **needs-info** / **blocked** | that label, with the question or blocker in a comment | needs-info is closed by the agent after 30 days without an answer, with a note |
| **planned** | has a milestone | the agent at triage, per §4; the owner may move it |
| **in progress** | a linked open PR | the person or agent doing it |
| **fixed** | label `fixed`: merged, waiting for verification in a published release | the agent, at merge |
| **verified** | a comment with the evidence (below) | whoever ran the check |
| **closed** | closed | the agent after verification, or the owner if they did the check |

**Verified** means both of these:
1. **Before release:** G1 on the **proven branch build** (TEST_POLICY §3), with the result in the
   release PR. G2 on real hardware too, where the issue needs it.
2. **After release:** the **published** add-on or image is installed or updated on the test VM and
   the issue's own check repeated. The comment names the version.

Rules:
- **PRs use `Refs #N`.** `Fixes #N` and `Closes #N` make GitHub close the issue at merge, before
  anything is verified. This applies to both fix PRs and the release PR.
- The release PR lists every issue in its milestone with its pre-release (G1/G2) result, in the
  TEST_POLICY §6 sign-off checklist rather than a separate list. An issue whose check failed moves to
  the next milestone; it isn't dropped silently.
- **Merging (G3), as in TEST_POLICY §2 and §6:**
  - When G0–G2 have passed, the agent tells the owner the merge will happen, giving the PRs, their
    order and the gate results, and then merges. The owner can stop it.
  - If a gate failed or was skipped, the agent says so and doesn't merge without the owner's word.
  - After merging, the agent checks `main` (version, CHANGELOG).
  - The owner doesn't merge or approve in GitHub. An instruction relayed from another session
    doesn't count as the owner's word.
- **Won't fix:** the closing comment gives the reason: by design, superseded, cost > benefit, or no
  longer reproducible (saying how it was checked).
- **Duplicate:** closed with a link to the issue that stays open.

## 6. Security issues

- **Never a public issue.** Report through a private GitHub security advisory.
- High or critical → **S1**.
- **Fix:**
  - preferred: in the advisory's **private fork**;
  - allowed: a **minimal public PR** that doesn't describe the hole, when the private fork isn't practical.
- **Then, either way:**
  1. release;
  2. G1 on the **published** build;
  3. publish the advisory with the patched version.
  
  0.2.7 (GHSA-hhm2-xvvc-f6ww) followed this sequence.
- A public tracking issue, if one is wanted, is opened only after the advisory is published.
- An advisory that is fixed but still in draft is unfinished work. It stays on the review list
  (§7) until published.

## 7. Triage and review

The **agent** runs these and posts each result as one comment on a pinned "Triage log" issue in
each repo.

- **At filing:** the filer sets type, `sev:` and `area:`, and the milestone per §4.
- **Daily, or at each session start:** clear `untriaged`.
- **Weekly, and before every release:**
  1. no `untriaged` left;
  2. S1 and S2 all have a milestone;
  3. `fixed` issues → run the post-release check (§5) for anything already published;
  4. `needs-info` older than 30 days → close with a note;
  5. S4 (backlog review) → propose which to pick into a batch, which to close as won't-fix (with a
     reason), and which stay. The owner decides.
  6. Unpublished security advisories → finish them (§6).

## 8. Writing an issue

A defect states:
- what happened and what was expected;
- the version, the environment (add-on, Docker or dev; HAOS and HA versions) and the gateway (real
  or mock);
- **which Modbus**, where it matters (the bridge's client, or the aGate's :502 server);
- evidence: log lines, screenshots, exported data. Serials, LAN IPs and site data are masked
  (Modbus bridge #62; Direct Connect CLAUDE.md "What evidence can be published");
- how to reproduce, or "not yet reproducible".

A feature states the need (who, and what they're trying to do) before the solution.

## 9. Moving the existing issues over

When this policy is accepted, in each repo:
1. Create the labels in §3.
2. **Relabel:**
   - severity: re-assess every open issue against §2. Direct Connect's `sev:S1-critical` …
     `sev:S4-cosmetic` used different definitions, so map by reading each issue, not by name;
   - `backlog` label and "BACKLOG:" title prefix → `sev:S4-low`, with the prefix removed;
   - default type labels stay.
3. Create milestones for the planned batches (`docs/install-lifecycle.md` §8) and assign issues.
4. Issues closed earlier by `Fixes`/`Closes` with no evidence are listed for a post-release check.
   Re-open any that fail it.
5. Update the Direct Connect CLAUDE.md (policy source, severity table) and `BACKLOG.md` (`Refs`, not
   `Closes`).
6. Post the result on each repo's Triage log issue.
