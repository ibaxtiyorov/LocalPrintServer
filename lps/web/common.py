"""Small helpers shared by the web views."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import jsonify, request


def ok(**data):
    return jsonify(ok=True, **data)


def fail(error: str, status: int = 400, **data):
    return jsonify(ok=False, error=error, **data), status


def page_args(default_per: int = 25) -> tuple[int, int]:
    try:
        page = max(1, min(10_000, int(request.args.get("page", 1))))
    except ValueError:
        page = 1
    try:
        per = max(5, min(200, int(request.args.get("per", default_per))))
    except ValueError:
        per = default_per
    return page, per


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def date_filters() -> tuple[str | None, str | None]:
    """?from=YYYY-MM-DD&to=YYYY-MM-DD (inclusive) -> ISO bounds (UTC day boundaries)."""
    f, t = _parse_date(request.args.get("from")), _parse_date(request.args.get("to"))
    return (f.strftime("%Y-%m-%dT00:00:00.000000Z") if f else None,
            (t + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000000Z") if t else None)


def json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}
