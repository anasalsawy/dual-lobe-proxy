"""All observer modes are advisory. Legacy mode names never enable holds."""
STAGES = ("observation", "context", "integrity-observe", "integrity-intervene", "enforcement")


def stage_index(stage: str) -> int:
    return STAGES.index((stage or "observation").strip().lower())


def at_least(current: str, required: str) -> bool:
    return stage_index(current) >= stage_index(required)


def injection_enabled(stage: str) -> bool:
    return stage_index(stage) > 0


def challenge_mode(stage: str) -> str:
    return "note" if injection_enabled(stage) else "none"
