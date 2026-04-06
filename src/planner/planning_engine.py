from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PlanStep:
    id: str
    title: str
    description: str
    status: str = "pending"


@dataclass(slots=True)
class PlanningResult:
    goal: str
    constraints: list[str]
    steps: list[PlanStep]
    exit_condition: str
    notes: list[str] = field(default_factory=list)


def build_plan(message: str) -> PlanningResult:
    lines = [line.strip(" -\t") for line in message.splitlines() if line.strip()]
    constraints = _extract_constraints(lines)
    explicit_steps = _extract_explicit_steps(lines)

    goal = _derive_goal(lines)
    if explicit_steps:
        steps = [
            PlanStep(
                id=f"s{index}",
                title=_condense_title(step),
                description=step,
            )
            for index, step in enumerate(explicit_steps, start=1)
        ]
    else:
        steps = _default_plan_steps(goal, constraints)

    notes: list[str] = []
    if constraints:
        notes.append("preserve listed constraints during execution")

    return PlanningResult(
        goal=goal,
        constraints=constraints,
        steps=steps,
        exit_condition="plan approved or first execution step started",
        notes=notes,
    )


def _derive_goal(lines: list[str]) -> str:
    if not lines:
        return "Clarify the user's objective"

    for line in lines:
        if not _is_step_line(line) and not _looks_like_constraint(line):
            return line.rstrip(".")
    return lines[0].rstrip(".")


def _extract_constraints(lines: list[str]) -> list[str]:
    constraints: list[str] = []
    for line in lines:
        lowered = line.lower()
        if _looks_like_constraint(line):
            constraints.append(line.rstrip("."))
            continue
        if any(token in lowered for token in ("must ", "should ", "without ", "avoid ", "제약", "반드시", "하지 말고")):
            constraints.append(line.rstrip("."))
    return _dedupe(constraints)


def _extract_explicit_steps(lines: list[str]) -> list[str]:
    steps = [line for line in lines if _is_step_line(line)]
    return [_strip_step_prefix(step).rstrip(".") for step in steps]


def _default_plan_steps(goal: str, constraints: list[str]) -> list[PlanStep]:
    return [
        PlanStep("s1", "Clarify objective", f"Confirm the target outcome for: {goal}"),
        PlanStep("s2", "Capture constraints", _constraints_description(constraints)),
        PlanStep("s3", "Sequence execution", "Break the work into ordered tasks with dependencies"),
        PlanStep("s4", "Define validation", "Specify how success will be checked before completion"),
    ]


def _constraints_description(constraints: list[str]) -> str:
    if not constraints:
        return "Identify technical, product, and time constraints"
    return "Keep these constraints visible: " + "; ".join(constraints)


def _is_step_line(line: str) -> bool:
    prefixes = tuple(f"{index}." for index in range(1, 10))
    bullet_prefixes = ("- ", "* ")
    return line.startswith(prefixes) or line.startswith(bullet_prefixes)


def _strip_step_prefix(line: str) -> str:
    if len(line) > 2 and line[0].isdigit() and line[1] == ".":
        return line[2:].strip()
    if line.startswith(("- ", "* ")):
        return line[2:].strip()
    return line.strip()


def _looks_like_constraint(line: str) -> bool:
    lowered = line.lower()
    return any(
        token in lowered
        for token in ("constraint", "constraints", "제약", "조건", "주의", "주의사항")
    )


def _condense_title(text: str) -> str:
    words = text.split()
    if not words:
        return "Untitled step"
    return " ".join(words[:5])


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
