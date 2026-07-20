from datetime import time

TIME_PRESETS: dict[str, tuple[str, time, time]] = {
    "ANY": ("Any time", time(0, 0), time(23, 59)),
    "MORNING": ("Morning", time(5, 0), time(11, 59)),
    "AFTERNOON": ("Afternoon", time(12, 0), time(16, 59)),
    "EVENING": ("Evening", time(17, 0), time(21, 59)),
    "NIGHT": ("Night", time(22, 0), time(23, 59)),
}


def canonical_preset(value: str | None) -> str:
    candidate = (value or "EVENING").strip().upper()
    return candidate if candidate in TIME_PRESETS or candidate == "CUSTOM" else "CUSTOM"


def label_for(value: str | None) -> str:
    preset = canonical_preset(value)
    if preset == "CUSTOM":
        return "Custom"
    return TIME_PRESETS[preset][0]


def range_for(value: str) -> tuple[time, time]:
    return TIME_PRESETS[canonical_preset(value)][1:]


def is_standard(value: str | None) -> bool:
    return canonical_preset(value) in TIME_PRESETS
