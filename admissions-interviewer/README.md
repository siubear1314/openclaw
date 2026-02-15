# admissions-interviewer

A custom OpenClaw skill for evaluating college applicants from:
- Resume text
- Interview transcript/video notes

## Files

- `SKILL.md` — skill instructions, workflow, output JSON schema, guardrails
- `references/rubric.md` — detailed scoring rubric and recommendation logic
- `README.md` — quick overview (this file)

## What it does

The skill scores candidates across core dimensions (communication, motivation, reflection, fit, leadership, integrity) using evidence-based quotes/timestamps, then produces:
- Structured scores
- Strengths and concerns
- A recommendation (`Admit`, `Borderline`, `Reject`, or `Insufficient Data`)

## Usage notes

1. Provide candidate resume text.
2. Provide interview transcript (timestamps preferred).
3. Optionally provide interviewer notes.
4. Run evaluation and review output with a human admissions committee.

## Important

This is a decision-support tool, not an autonomous admissions decision-maker.
Always include human review.
