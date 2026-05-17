# Build Brief — Pre-flight Workflow

When this command is invoked, follow this workflow in full before writing any code.
Do not skip phases. Do not start building until the user approves Phase 3.

---

## Phase 1 — Complete the Brief

If the user's request is missing any of the following, ask for them now in a single
consolidated question (do not ask one at a time):

- **What**: Core feature AND every edge case or sub-use-case they can think of
- **Who**: Who uses it and their technical level (non-technical, developer, etc.)
- **Where**: Exact OS, hardware, version, browser, or runtime environment
  (e.g. "macOS Sonoma on Intel Mac mini" not just "Mac")
- **Client devices**: Every device type that will interact with it and how
  (e.g. "iPhone browser", "Windows laptop", "iPad touch")
- **When**: Is this attended (someone watching) or unattended (background/scheduled)?
  Runs once or continuously?
- **Done criteria**: How does the user know it is working correctly?
- **Failure criteria**: What is the worst acceptable failure mode?

Do not assume. If anything is ambiguous, ask.

---

## Phase 2 — Pre-flight Analysis

Before any design or code, produce a pre-flight analysis with these five sections.
Be specific — generic answers are not acceptable.

### 1. Platform Gotchas
List every known limitation, quirk, or non-obvious behaviour of the target
platform(s) that affects this design. Examples of the kind of thing to list:
- OS-level restrictions (sandboxing, permissions, PATH differences in background services)
- Browser or runtime limitations (what APIs are unavailable, what behaves differently)
- Hardware variation (Apple Silicon vs Intel, Retina scaling, touch vs mouse)
- Known library limitations for the chosen stack on this platform

### 2. Permission and Dependency Map
List everything that must be installed, configured, granted, or permitted before
this works. For each item state: what it is, how to get it, and what happens if
it is missing.

### 3. Failure Modes
For each dependency and integration point, state what the user will see if it
is missing, misconfigured, or unavailable. Include the exact error or symptom.

### 4. Order-of-Operations Risks
List any steps that will fail if done twice, out of order, skipped, or run
before a prerequisite. Include what happens if the user re-runs the install
command when already installed.

### 5. Environment Differences
List anything that behaves differently between the development/build environment
and where it will actually run (e.g. shell PATH vs service PATH, local vs CI,
attended terminal vs background daemon).

---

## Phase 3 — Dual Design (get approval before coding)

Produce two separate designs and present both to the user for approval:

### A. Technical Architecture
What the code will do: components, data flow, key libraries, how parts connect.

### B. User Journey
What the human experiences, step by step:
- First-run / setup (every prompt, every screen, every permission dialog)
- Normal daily use
- What happens when something goes wrong (exact error messages and recovery steps)
- What happens if any step is done twice or out of order

**Stop here. Ask the user: "Does this plan look right? Any changes before I build?"**
Do not proceed to Phase 4 until the user approves.

---

## Phase 4 — Build

Write the code. After completing, answer these questions without being asked:
- What edge cases are not handled?
- What assumptions did you make that the user did not state?
- What would fail on first run if the user skips any setup step?

---

## Phase 5 — Instructions Review

After writing the setup/usage instructions, self-review them by answering:
- Which step will most users fail on, and why?
- What prerequisite knowledge is assumed that a non-technical user would not have?
- What is missing that would cause a user to get stuck?

Fix any issues found before presenting the instructions to the user.
