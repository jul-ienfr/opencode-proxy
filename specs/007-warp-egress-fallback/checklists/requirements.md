# Specification Quality Checklist: WARP primaire + replis d'egress free

**Purpose**: Validate specification completeness and quality before planning
**Created**: 2026-11-07
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs) leak into the
      behavioral requirements; the plan carries the technical stack.
- [x] Focused on user value and business needs (capacity, continuity,
      free-first, visibility).
- [x] Written for technical stakeholders; project-specific terms are defined
      in Key Entities and the plan.
- [x] All mandatory sections completed.

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain; Phase 0 is the explicit
      measurement step for the main unknown.
- [x] Requirements are testable and unambiguous (FR-001 → FR-025).
- [x] Success criteria are measurable (SC-001 → SC-010).
- [x] Success criteria are technology-agnostic where possible; provider names
      are business terms of this project.
- [x] All acceptance scenarios are defined for US1–US5.
- [x] Edge cases are identified (403, rate-limit, reset jitter, duplicate
      blocks, family mismatch, pool empty, prefix change).
- [x] Scope is clearly bounded: WARP primary, free fallback, VPS optional.
- [x] Dependencies and assumptions identified.

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria or a
      dedicated test task.
- [x] User scenarios cover primary flows (park/rotate, fallback/return,
      Phase 0, GUI, VPS).
- [x] Feature meets measurable outcomes defined in Success Criteria.
- [x] No implementation details leak into the specification.

## Notes

- The main unresolved measurement is the quota granularity (address vs `/64`
  vs ASN/account/global vs model). It is deliberately handled by US3/Phase 0
  rather than a [NEEDS CLARIFICATION] marker, because only a live measurement
  can answer it.
- The VPS `/48` is explicitly optional, paid and disabled by default; it does
  not block the MVP.
