---
name: admissions-interviewer
description: Evaluate college applicants using a structured admissions interview rubric from resume content and interview transcripts/video notes. Use when screening candidates, scoring interview performance, generating evidence-based admit/borderline/reject recommendations, or producing consistent interviewer-style feedback with bias-safe guardrails.
---

# Admissions Interviewer

Use this skill to run structured, evidence-based candidate evaluations from:
- Resume text
- Interview transcript (preferred, with timestamps)
- Optional interviewer notes (behavioral/nonverbal observations)

## Required Inputs

Collect these before scoring:
1. Candidate identifier (name or ID)
2. Resume text (raw or parsed)
3. Interview transcript (with timestamps when available)
4. Context (program, major, institution type, round)

If transcript quality is poor or missing major sections, return **Insufficient Data** for affected categories.

## Evaluation Rules

1. Score only from observed evidence.
2. Quote or cite evidence for every category.
3. Do not infer protected attributes (race, religion, disability, etc.).
4. Separate facts from interpretation.
5. Prefer conservative scoring when evidence is thin.
6. Mark confidence level (High/Medium/Low).
7. Recommendation is advisory; require human committee review.

## Scoring Framework

Use the rubric in `references/rubric.md`.

Core categories (1-10 each):
- Communication Clarity
- Motivation & Purpose
- Self-Awareness & Reflection
- Academic/Program Fit
- Leadership & Initiative
- Integrity & Professionalism

Optional category (if needed):
- Resilience & Adaptability

## Output Format

Return concise narrative plus structured JSON.

### Narrative (short)
- Top strengths (2-4 bullets)
- Main concerns (1-3 bullets)
- Final recommendation + rationale (2-4 lines)

### JSON Template

```json
{
  "candidate_id": "",
  "context": {
    "program": "",
    "round": "",
    "institution": ""
  },
  "scores": {
    "communication_clarity": 0,
    "motivation_purpose": 0,
    "self_awareness_reflection": 0,
    "academic_program_fit": 0,
    "leadership_initiative": 0,
    "integrity_professionalism": 0,
    "resilience_adaptability": null
  },
  "evidence": [
    {
      "category": "motivation_purpose",
      "quote": "",
      "timestamp": "",
      "source": "interview_transcript|resume|notes"
    }
  ],
  "strengths": [],
  "concerns": [],
  "recommendation": "Admit|Borderline|Reject|Insufficient Data",
  "confidence": "High|Medium|Low",
  "bias_safety_note": "Evaluation excludes protected-attribute inference and should be reviewed by human committee."
}
```

## Decision Guidance

Use this as soft guidance (not hard automation):
- **Admit**: strong evidence across most categories, no major integrity concerns.
- **Borderline**: mixed profile, notable upside with clear gaps.
- **Reject**: substantial concerns in readiness/fit/integrity with limited mitigating evidence.
- **Insufficient Data**: missing or unreliable interview evidence.

## Handling Missing or Conflicting Evidence

- If resume and interview conflict, flag explicitly and lower confidence.
- If transcript has unclear audio/ASR artifacts, avoid over-interpreting tone.
- If no timestamp is available, still include exact quote and source.

## Quality Checklist

Before finalizing:
- Every score has at least one evidence item.
- Concerns are specific and actionable.
- Recommendation matches score pattern and evidence.
- Output is concise and readable for admissions committee use.
