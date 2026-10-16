# Issue policy: classifying, prioritising and closing issues

Status: **draft for review**, 2026-10-16.

Applies to **franklinwh-modbus-bridge** and **franklinwh-direct-connect-bridge**. This repo's `docs/`
is the home for the shared docs; the Direct Connect repo links here.

It completes the other two policies:

| Policy | Says |
|---|---|
| This document | what an issue is, how bad it is, when it gets done, and when it's finished |
| `docs/install-lifecycle.md` | what install, update and uninstall must do to HA and MQTT |
| `docs/TEST_POLICY.md` (shared) | what a change must pass before it ships |

## 1. Every issue gets four labels at filing time

Whoever files the issue labels it: owner, contributor or agent. An unlabelled issue is untriaged
(§4).

### Type: what kind of work

| Label | Meaning |
|---|---|
| `type:defect` | Something doesn't do what it should, or does harm |
| `type:feature` | New capability, or a change to agreed behaviour |
| `type:docs` | Documentation only |
| `type:chore` | Housekeeping: dependencies, CI, tooling, refactors with no behaviour change |

Security problems are **not** filed as public issues. They go to a private GitHub security advisory
first (§6).

### Severity: how bad it is if it isn't fixed (defects only)

| Label | Meaning | Examples of the kind |
|---|---|---|
| `sev:1-critical` | Safety or security; wrong battery control; data loss; the bridge unusable for everyone | a command does something other than what was asked; unauthenticated admin access |
| `sev:2-major` | A feature is broken, with no reasonable workaround | an action reports success but leaves the result wrong |
| `sev:3-minor` | Wrong, but there's a workaround or the impact is limited | a chart glitch from a bad sample; a misleading message |
| `sev:4-cosmetic` | Looks wrong; nothing behaves wrongly | layout, wording, colour |

When unsure between two levels, pick the **higher** one and say why in the issue.

### Priority: when it gets done

| Label | Meaning |
|---|---|
| `pri:0-now` | Hotfix. Other work stops until it ships. |
| `pri:1-next` | In the next release. |
| `pri:2-planned` | On the roadmap; has a milestone further out. |
| `pri:3-someday` | Nice to have. Revisited at review (§5); closed if it never earns a slot. |

**Severity sets the floor:**
- `sev:1` is always `pri:0`.
- `sev:2` is at least `pri:1`, unless the issue states why not and the owner agrees in that issue.
- Features and docs have no severity; their priority is the owner's call.

### Area: which part of the system

These match the release areas in the test policy, so "one functional area per release" can be read
straight off the labels:

`area:store` · `area:poller` · `area:modbus` · `area:mqtt-identity` · `area:lifecycle` ·
`area:wizard-ui` · `area:api` · `area:scheduler-dispatch` · `area:packaging` · `area:docs`

An issue spanning two areas gets both. Its fix is split so that each release still covers one
area.

## 2. Lifecycle of an issue

```
new → triaged → planned → in progress → fixed → verified → closed
         └──→ needs-info        └──→ won't fix (with reason)
```

| State | How you can tell | Who moves it on |
|---|---|---|
| **new** | no labels | whoever filed it, at filing time |
| **triaged** | type, severity (defects), priority and area set | filer; owner adjusts |
| **needs-info** | label `needs-info`; the question is in a comment | closed after 30 days without an answer, with a note |
| **planned** | has a milestone (a release, §3) | owner, at review |
| **in progress** | linked PR open | the person or agent doing it |
| **fixed** | PR merged | the merge |
| **verified** | the fix was seen working in the shipped build: the G1 acceptance run, or G2 on real hardware where the issue needs it. Evidence is in a comment. | whoever ran the check |
| **closed** | only after **verified**, or as won't-fix/duplicate | the verifier |

Rules:
- **Every defect gets an issue,** including ones found during other work. Don't fix things silently.
- **"Fixed" is not "closed".** A defect closes when the evidence shows it's gone in the build users
  get, not when the PR merges. A PR uses `Refs #N`, not `Fixes #N`, so the merge doesn't close the
  issue automatically.
- **Won't fix** needs a reason in the closing comment (by design, superseded, cost > benefit, or no
  longer reproducible, with how it was checked). Label `wontfix`.
- **Duplicates** are closed with a link to the issue that stays open. Label `duplicate`.
- **Regression:** a defect that a previous release didn't have gets the label `regression` and the
  version that introduced it, and is treated **one severity level higher** for priority.

## 3. Milestones are releases

- Each planned release is a GitHub milestone (`0.2.8`, `0.3.0`, …). The release order follows
  `docs/install-lifecycle.md` §8.
- A milestone holds **one area's** work, plus any `pri:0` hotfix that must go with it (test policy:
  one functional area per release, identity changes alone).
- The release PR lists every issue in its milestone and states, for each, whether it was verified.
  An issue that isn't verified moves to the next milestone; it isn't dropped silently.

## 4. Triage

- **At filing:** the filer sets type, severity, priority and area. Agents do this too, as part of filing.
- **Within a day:** the owner confirms or changes them. A change of severity or priority gets a
  one-line reason in the issue.
- **Untriaged list:** issues with no `pri:` label. It should be empty after each review.

## 5. Review

Weekly, or before cutting any release:
1. Untriaged → triaged.
2. `pri:0` and `pri:1` → each has an owner and a milestone.
3. `needs-info` older than 30 days → close with a note.
4. `pri:3-someday` older than 90 days → keep only if someone can say why it still matters; otherwise
   close as won't-fix with that reason.
5. Open `sev:1`/`sev:2` without a milestone → there shouldn't be any.

## 6. Security issues

- Report privately as a GitHub security advisory, never as a public issue or PR comment.
- Severity follows the advisory's rating; anything rated high or critical is `pri:0`.
- Fix in the advisory's private fork, release, run G1 on the released build, then publish the
  advisory with the patched version (the 0.2.7 / GHSA-hhm2-xvvc-f6ww sequence).
- A public tracking issue, if one is wanted, is opened only after the advisory is published.

## 7. Writing an issue

A defect states:
- what happened vs what was expected;
- version, environment (add-on, Docker or dev; HAOS and HA versions) and the gateway (real or mock);
- evidence: log lines, screenshots, exported data, with serials, LAN IPs and site data masked
  (PII policy);
- how to reproduce, or "not yet reproducible".

A feature states the need (who, and what they're trying to do) before the solution.

## 8. Moving the existing issues over

When this policy is accepted:
1. Create the labels (§1) in both repos.
2. Label every open issue. Replace the "BACKLOG:" title prefix with `pri:3-someday` or `pri:2-planned`.
3. Create milestones for the releases already planned (`docs/install-lifecycle.md` §8) and assign
   issues to them.
4. Post the result as one comment on the tracking issue for this policy.
