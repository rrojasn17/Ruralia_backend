"""Madurez estimada: no sustituye el estado operativo ni confirma una cosecha."""

from datetime import timedelta


def harvest_timing(planted_date, cycle_weeks, today):
    age = max(0, (today - planted_date).days)
    due = (
        planted_date + timedelta(weeks=cycle_weeks)
        if cycle_weeks and cycle_weeks > 0
        else None
    )
    remaining = (due - today).days if due else None
    status = (
        "ready"
        if remaining is not None and remaining <= 0
        else "upcoming"
        if remaining is not None and remaining <= 21
        else "growing"
    )
    return {
        "weeks_elapsed": age // 7,
        "cycle_weeks": cycle_weeks,
        "harvest_due_date": due,
        "days_to_harvest": remaining,
        "readiness": status,
    }
