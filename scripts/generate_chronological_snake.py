#!/usr/bin/env python3
"""Generate a chronological, growing GitHub contribution snake as animated SVG.

The snake visits contribution-calendar dates in ascending order. Empty days are
traversed quickly; active contribution days are eaten in strict date order. Each
active day increases the target body length by one segment.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import random
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

API_URL = "https://api.github.com/graphql"
USER_AGENT = "7toCR-chronological-contribution-snake/1.0"

LEVELS = (
    "NONE",
    "FIRST_QUARTILE",
    "SECOND_QUARTILE",
    "THIRD_QUARTILE",
    "FOURTH_QUARTILE",
)

LIGHT_PALETTE = {
    "background": "#ffffff",
    "text": "#24292f",
    "muted": "#57606a",
    "empty": "#ebedf0",
    "NONE": "#ebedf0",
    "FIRST_QUARTILE": "#9be9a8",
    "SECOND_QUARTILE": "#40c463",
    "THIRD_QUARTILE": "#30a14e",
    "FOURTH_QUARTILE": "#216e39",
    "snake": "#2563EB",
    "snake_outline": "#1E40AF",
    "eye": "#ffffff",
    "pupil": "#111827",
    "connector": "#d0d7de",
}

DARK_PALETTE = {
    "background": "#0d1117",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "empty": "#161b22",
    "NONE": "#161b22",
    "FIRST_QUARTILE": "#0e4429",
    "SECOND_QUARTILE": "#006d32",
    "THIRD_QUARTILE": "#26a641",
    "FOURTH_QUARTILE": "#39d353",
    "snake": "#A78BFA",
    "snake_outline": "#6D28D9",
    "eye": "#ffffff",
    "pupil": "#111827",
    "connector": "#30363d",
}


@dataclass(frozen=True)
class ContributionDay:
    date: dt.date
    count: int
    level: str

    @property
    def active(self) -> bool:
        return self.count > 0


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class TimelineEntry:
    day: ContributionDay
    point: Point
    distance: float
    key_time: float
    active_eaten: int


class GitHubGraphQLError(RuntimeError):
    pass


def graphql_request(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubGraphQLError(f"GitHub GraphQL HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubGraphQLError(f"GitHub GraphQL request failed: {exc}") from exc

    parsed = json.loads(body)
    if parsed.get("errors"):
        raise GitHubGraphQLError(json.dumps(parsed["errors"], ensure_ascii=False))
    return parsed.get("data", {})


def fetch_contribution_years(username: str, token: str) -> list[int]:
    query = """
    query ContributionYears($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionYears
        }
      }
    }
    """
    data = graphql_request(token, query, {"login": username})
    user = data.get("user")
    if user is None:
        raise GitHubGraphQLError(f"GitHub user not found: {username}")
    years = user["contributionsCollection"]["contributionYears"]
    return sorted({int(year) for year in years})


def fetch_year(username: str, token: str, year: int, today: dt.date) -> list[ContributionDay]:
    start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
    end_date = min(today, dt.date(year, 12, 31))
    end = dt.datetime.combine(end_date, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)

    query = """
    query ContributionCalendar($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
                contributionLevel
              }
            }
          }
        }
      }
    }
    """
    variables = {
        "login": username,
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
    }
    data = graphql_request(token, query, variables)
    user = data.get("user")
    if user is None:
        raise GitHubGraphQLError(f"GitHub user not found: {username}")

    calendar = user["contributionsCollection"]["contributionCalendar"]
    result: list[ContributionDay] = []
    for week in calendar["weeks"]:
        for raw in week["contributionDays"]:
            date = dt.date.fromisoformat(raw["date"])
            if date.year != year or date > today:
                continue
            level = raw.get("contributionLevel", "NONE")
            if level not in LEVELS:
                level = "NONE"
            result.append(
                ContributionDay(
                    date=date,
                    count=int(raw.get("contributionCount", 0)),
                    level=level,
                )
            )
    return sorted(result, key=lambda item: item.date)


def fill_calendar_days(days: Iterable[ContributionDay], first_year: int, today: dt.date) -> list[ContributionDay]:
    by_date = {day.date: day for day in days}
    cursor = dt.date(first_year, 1, 1)
    result: list[ContributionDay] = []
    while cursor <= today:
        result.append(by_date.get(cursor, ContributionDay(cursor, 0, "NONE")))
        cursor += dt.timedelta(days=1)
    return result


def load_github_days(username: str, token: str, max_years: int, today: dt.date) -> list[ContributionDay]:
    years = [year for year in fetch_contribution_years(username, token) if year <= today.year]
    if not years:
        years = [today.year]
    if max_years > 0:
        years = years[-max_years:]

    fetched: list[ContributionDay] = []
    for year in years:
        print(f"Fetching contribution calendar for {year}...", file=sys.stderr)
        fetched.extend(fetch_year(username, token, year, today))

    return fill_calendar_days(fetched, min(years), today)


def generate_demo_days(today: dt.date) -> list[ContributionDay]:
    """Create deterministic sample data for local preview and validation."""
    rng = random.Random(7)
    start = dt.date(today.year - 1, 1, 1)
    result: list[ContributionDay] = []
    cursor = start
    while cursor <= today:
        weekday = cursor.weekday()
        seasonal = 0.18 + 0.10 * math.sin(cursor.timetuple().tm_yday / 17.0)
        probability = seasonal + (0.08 if weekday < 5 else -0.06)
        if rng.random() < max(0.03, probability):
            count = rng.randint(1, 14)
            if count <= 3:
                level = "FIRST_QUARTILE"
            elif count <= 6:
                level = "SECOND_QUARTILE"
            elif count <= 10:
                level = "THIRD_QUARTILE"
            else:
                level = "FOURTH_QUARTILE"
        else:
            count = 0
            level = "NONE"
        result.append(ContributionDay(cursor, count, level))
        cursor += dt.timedelta(days=1)
    return result


def first_sunday(year: int) -> dt.date:
    jan1 = dt.date(year, 1, 1)
    days_since_sunday = (jan1.weekday() + 1) % 7
    return jan1 - dt.timedelta(days=days_since_sunday)


def escape(value: str) -> str:
    return html.escape(value, quote=True)


def fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def polyline_length(points: Sequence[Point]) -> tuple[float, list[float]]:
    if not points:
        return 0.0, []
    cumulative = [0.0]
    total = 0.0
    for previous, current in zip(points, points[1:]):
        total += math.hypot(current.x - previous.x, current.y - previous.y)
        cumulative.append(total)
    return total, cumulative


def svg_path(points: Sequence[Point]) -> str:
    if not points:
        return ""
    parts = [f"M {fmt(points[0].x)} {fmt(points[0].y)}"]
    parts.extend(f"L {fmt(point.x)} {fmt(point.y)}" for point in points[1:])
    return " ".join(parts)


def build_layout(days: Sequence[ContributionDay], cell: int, gap: int) -> tuple[
    dict[dt.date, Point], dict[int, float], int, int, float, float
]:
    pitch = cell + gap
    years = sorted({day.date.year for day in days})
    title_height = 64
    label_left = 68
    right_padding = 34
    month_label_height = 18
    year_gap = 42
    grid_height = 7 * pitch - gap

    max_week = 0
    for year in years:
        start = first_sunday(year)
        end = max(day.date for day in days if day.date.year == year)
        max_week = max(max_week, (end - start).days // 7)

    grid_width = (max_week + 1) * pitch - gap
    width = int(label_left + grid_width + right_padding)
    row_step = month_label_height + grid_height + year_gap
    height = int(title_height + len(years) * row_step + 12)

    year_tops: dict[int, float] = {}
    coordinates: dict[dt.date, Point] = {}
    for index, year in enumerate(years):
        grid_top = title_height + index * row_step + month_label_height
        year_tops[year] = grid_top
        start = first_sunday(year)
        for day in (item for item in days if item.date.year == year):
            week = (day.date - start).days // 7
            weekday_sunday_zero = (day.date.weekday() + 1) % 7
            coordinates[day.date] = Point(
                x=label_left + week * pitch + cell / 2,
                y=grid_top + weekday_sunday_zero * pitch + cell / 2,
            )

    return coordinates, year_tops, width, height, label_left, grid_width


def build_route(
    route_days: Sequence[ContributionDay],
    coordinates: dict[dt.date, Point],
    label_left: float,
    grid_width: float,
    connector_margin: float,
) -> tuple[list[Point], dict[dt.date, float], float]:
    if not route_days:
        raise ValueError("No route days")

    points: list[Point] = [coordinates[route_days[0].date]]
    date_point_indices = {route_days[0].date: 0}
    previous_day = route_days[0]

    for day in route_days[1:]:
        point = coordinates[day.date]
        previous_point = coordinates[previous_day.date]
        if day.date.year != previous_day.date.year:
            right_x = label_left + grid_width + connector_margin
            left_x = label_left - connector_margin
            bridge_y = point.y - 16
            points.extend(
                [
                    Point(right_x, previous_point.y),
                    Point(right_x, bridge_y),
                    Point(left_x, bridge_y),
                    Point(left_x, point.y),
                ]
            )
        points.append(point)
        date_point_indices[day.date] = len(points) - 1
        previous_day = day

    total, cumulative = polyline_length(points)
    date_distances = {
        date: cumulative[index]
        for date, index in date_point_indices.items()
    }
    return points, date_distances, total


def build_timeline(
    route_days: Sequence[ContributionDay],
    coordinates: dict[dt.date, Point],
    date_distances: dict[dt.date, float],
    duration: float,
    start_hold: float,
    end_hold: float,
    empty_weight: float,
    active_weight: float,
    year_transition_weight: float,
) -> list[TimelineEntry]:
    if duration <= start_hold + end_hold:
        raise ValueError("duration must exceed start_hold + end_hold")

    first = route_days[0]
    movement_duration = duration - start_hold - end_hold
    transition_weights: list[float] = []
    for previous, current in zip(route_days, route_days[1:]):
        weight = active_weight if current.active else empty_weight
        if previous.date.year != current.date.year:
            weight += year_transition_weight
        transition_weights.append(weight)
    total_weight = sum(transition_weights) or 1.0

    entries: list[TimelineEntry] = []
    active_eaten = 0

    # Initial state: head waits over the oldest contribution, before eating it.
    entries.append(
        TimelineEntry(
            day=first,
            point=coordinates[first.date],
            distance=date_distances[first.date],
            key_time=0.0,
            active_eaten=0,
        )
    )

    if first.active:
        active_eaten += 1
    entries.append(
        TimelineEntry(
            day=first,
            point=coordinates[first.date],
            distance=date_distances[first.date],
            key_time=start_hold / duration,
            active_eaten=active_eaten,
        )
    )

    accumulated = 0.0
    for index, day in enumerate(route_days[1:]):
        accumulated += transition_weights[index]
        seconds = start_hold + movement_duration * accumulated / total_weight
        if day.active:
            active_eaten += 1
        entries.append(
            TimelineEntry(
                day=day,
                point=coordinates[day.date],
                distance=date_distances[day.date],
                key_time=seconds / duration,
                active_eaten=active_eaten,
            )
        )

    final = entries[-1]
    entries.append(
        TimelineEntry(
            day=final.day,
            point=final.point,
            distance=final.distance,
            key_time=1.0,
            active_eaten=final.active_eaten,
        )
    )
    return entries


def month_labels_for_year(year: int, available_dates: set[dt.date], coordinates: dict[dt.date, Point]) -> list[tuple[str, float]]:
    labels: list[tuple[str, float]] = []
    for month in range(1, 13):
        candidates = sorted(date for date in available_dates if date.year == year and date.month == month)
        if not candidates:
            continue
        first = candidates[0]
        labels.append((first.strftime("%b"), coordinates[first].x - 4))
    return labels


def render_svg(
    username: str,
    days: Sequence[ContributionDay],
    theme: str,
    duration: float,
    cell: int,
    gap: int,
    base_body: float,
    growth_per_active_day: float,
    snake_color: str | None = None,
    snake_outline: str | None = None,
) -> str:
    palette = dict(LIGHT_PALETTE if theme == "light" else DARK_PALETTE)
    if snake_color:
        palette["snake"] = snake_color
    if snake_outline:
        palette["snake_outline"] = snake_outline
    active_days = [day for day in days if day.active]
    if not active_days:
        raise ValueError("No active contribution days were found")

    first_active = active_days[0].date
    route_days = [day for day in days if first_active <= day.date <= days[-1].date]
    coordinates, year_tops, width, height, label_left, grid_width = build_layout(days, cell, gap)
    route_points, date_distances, total_path_length = build_route(
        route_days,
        coordinates,
        label_left,
        grid_width,
        connector_margin=22,
    )
    timeline = build_timeline(
        route_days,
        coordinates,
        date_distances,
        duration=duration,
        start_hold=1.2,
        end_hold=1.6,
        empty_weight=0.10,
        active_weight=1.0,
        year_transition_weight=1.6,
    )

    path_d = svg_path(route_points)
    key_times = ";".join(fmt(entry.key_time) for entry in timeline)
    key_points = ";".join(fmt(entry.distance / total_path_length) for entry in timeline)

    dash_arrays: list[str] = []
    dash_offsets: list[str] = []
    for entry in timeline:
        target_body = base_body + growth_per_active_day * entry.active_eaten
        visible_body = min(target_body, entry.distance)
        tail = max(0.0, entry.distance - visible_body)
        dash_arrays.append(f"{fmt(max(0.01, visible_body))} {fmt(total_path_length + 1)}")
        dash_offsets.append(fmt(-tail))

    active_eat_times: dict[dt.date, float] = {}
    for entry in timeline[1:-1]:
        if entry.day.active and entry.day.date not in active_eat_times:
            active_eat_times[entry.day.date] = entry.key_time

    years = sorted({day.date.year for day in days})
    available_dates = {day.date for day in days}
    total_contributions = sum(day.count for day in days)
    total_active_days = len(active_days)

    out: list[str] = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(username)} chronological contribution snake">'
    )
    out.append("<title>Chronological GitHub contribution snake</title>")
    out.append(
        f"<desc>The snake eats {total_active_days} active contribution days from "
        f"{first_active.isoformat()} to {days[-1].date.isoformat()} in chronological order.</desc>"
    )
    out.append("<defs>")
    out.append(
        f'<filter id="snake-shadow" x="-30%" y="-30%" width="160%" height="160%">'
        f'<feDropShadow dx="0" dy="1.5" stdDeviation="1.4" flood-color="{palette["snake_outline"]}" flood-opacity="0.45"/>'
        "</filter>"
    )
    out.append("</defs>")
    out.append(f'<rect width="100%" height="100%" rx="12" fill="{palette["background"]}"/>')

    out.append(
        f'<text x="18" y="26" fill="{palette["text"]}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        f'font-size="16" font-weight="600">{escape(username)} · Chronological Contribution Snake</text>'
    )
    out.append(
        f'<text x="18" y="47" fill="{palette["muted"]}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        f'font-size="11">oldest to newest · {total_active_days} active days · {total_contributions} contributions</text>'
    )

    # Calendar rows and month labels.
    for year in years:
        grid_top = year_tops[year]
        out.append(
            f'<text x="12" y="{fmt(grid_top + 10)}" fill="{palette["muted"]}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="11" font-weight="600">'
            f'{year}</text>'
        )
        for label, x in month_labels_for_year(year, available_dates, coordinates):
            out.append(
                f'<text x="{fmt(x)}" y="{fmt(grid_top - 7)}" fill="{palette["muted"]}" '
                f'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="9">{label}</text>'
            )

    # Contribution cells. Active cells turn empty exactly when eaten.
    for day in days:
        point = coordinates[day.date]
        x = point.x - cell / 2
        y = point.y - cell / 2
        original_fill = palette.get(day.level, palette["empty"])
        title = f"{day.date.isoformat()}: {day.count} contribution{'s' if day.count != 1 else ''}"
        out.append(
            f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{cell}" height="{cell}" rx="2" '
            f'fill="{original_fill}"><title>{escape(title)}</title>'
        )
        if day.active and day.date in active_eat_times:
            eat_time = active_eat_times[day.date]
            before = max(0.0, eat_time - 0.001)
            after = min(1.0, eat_time + 0.001)
            out.append(
                f'<animate attributeName="fill" dur="{fmt(duration)}s" repeatCount="indefinite" '
                f'values="{original_fill};{original_fill};{palette["empty"]};{palette["empty"]}" '
                f'keyTimes="0;{fmt(before)};{fmt(after)};1" calcMode="discrete"/>'
            )
        out.append("</rect>")

    # Subtle guide route, then animated growing snake body.
    out.append(
        f'<path d="{path_d}" fill="none" stroke="{palette["connector"]}" stroke-width="1" '
        'stroke-linecap="round" stroke-linejoin="round" opacity="0.16"/>'
    )
    out.append(
        f'<path d="{path_d}" fill="none" stroke="{palette["snake_outline"]}" stroke-width="10" '
        'stroke-linecap="round" stroke-linejoin="round" opacity="0.55" filter="url(#snake-shadow)" '
        f'stroke-dasharray="{dash_arrays[0]}" stroke-dashoffset="{dash_offsets[0]}">'
    )
    out.append(
        f'<animate attributeName="stroke-dasharray" dur="{fmt(duration)}s" repeatCount="indefinite" '
        f'values="{";".join(dash_arrays)}" keyTimes="{key_times}" calcMode="linear"/>'
    )
    out.append(
        f'<animate attributeName="stroke-dashoffset" dur="{fmt(duration)}s" repeatCount="indefinite" '
        f'values="{";".join(dash_offsets)}" keyTimes="{key_times}" calcMode="linear"/>'
    )
    out.append("</path>")

    out.append(
        f'<path d="{path_d}" fill="none" stroke="{palette["snake"]}" stroke-width="7" '
        'stroke-linecap="round" stroke-linejoin="round" '
        f'stroke-dasharray="{dash_arrays[0]}" stroke-dashoffset="{dash_offsets[0]}">'
    )
    out.append(
        f'<animate attributeName="stroke-dasharray" dur="{fmt(duration)}s" repeatCount="indefinite" '
        f'values="{";".join(dash_arrays)}" keyTimes="{key_times}" calcMode="linear"/>'
    )
    out.append(
        f'<animate attributeName="stroke-dashoffset" dur="{fmt(duration)}s" repeatCount="indefinite" '
        f'values="{";".join(dash_offsets)}" keyTimes="{key_times}" calcMode="linear"/>'
    )
    out.append("</path>")

    # Animated head with auto-rotation and simple eyes.
    out.append('<g filter="url(#snake-shadow)">')
    out.append(f'<circle cx="0" cy="0" r="6.4" fill="{palette["snake_outline"]}"/>')
    out.append(f'<circle cx="0" cy="0" r="5.2" fill="{palette["snake"]}"/>')
    out.append(f'<circle cx="2.2" cy="-2.1" r="1.25" fill="{palette["eye"]}"/>')
    out.append(f'<circle cx="2.2" cy="2.1" r="1.25" fill="{palette["eye"]}"/>')
    out.append(f'<circle cx="2.7" cy="-2.1" r="0.55" fill="{palette["pupil"]}"/>')
    out.append(f'<circle cx="2.7" cy="2.1" r="0.55" fill="{palette["pupil"]}"/>')
    out.append(
        f'<animateMotion dur="{fmt(duration)}s" repeatCount="indefinite" rotate="auto" '
        f'path="{path_d}" keyPoints="{key_points}" keyTimes="{key_times}" calcMode="linear"/>'
    )
    out.append("</g>")

    out.append("</svg>")
    return "\n".join(out) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("GITHUB_USERNAME", "7toCR"))
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--duration", type=float, default=60.0, help="Animation duration in seconds")
    parser.add_argument("--max-years", type=int, default=0, help="0 means all contribution years")
    parser.add_argument("--cell-size", type=int, default=10)
    parser.add_argument("--cell-gap", type=int, default=3)
    parser.add_argument("--base-body", type=float, default=34.0)
    parser.add_argument("--growth-per-active-day", type=float, default=7.0)
    parser.add_argument("--light-snake", default="#2563EB")
    parser.add_argument("--light-outline", default="#1E40AF")
    parser.add_argument("--dark-snake", default="#A78BFA")
    parser.add_argument("--dark-outline", default="#6D28D9")
    parser.add_argument("--demo", action="store_true", help="Generate deterministic preview data without GitHub API")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration < 8:
        raise SystemExit("--duration must be at least 8 seconds")
    if args.cell_size < 5 or args.cell_gap < 1:
        raise SystemExit("cell size/gap are too small")

    if args.demo:
        days = generate_demo_days(args.today)
    else:
        if not args.token:
            raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required unless --demo is used")
        days = load_github_days(args.username, args.token, args.max_years, args.today)

    days = sorted(days, key=lambda item: item.date)
    active = [day for day in days if day.active]
    if not active:
        raise SystemExit("No active contribution days found; nothing to animate")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "chronological-contribution-snake.svg": "light",
        "chronological-contribution-snake-dark.svg": "dark",
    }
    for filename, theme in outputs.items():
        svg = render_svg(
            username=args.username,
            days=days,
            theme=theme,
            duration=args.duration,
            cell=args.cell_size,
            gap=args.cell_gap,
            base_body=args.base_body,
            growth_per_active_day=args.growth_per_active_day,
            snake_color=args.light_snake if theme == "light" else args.dark_snake,
            snake_outline=args.light_outline if theme == "light" else args.dark_outline,
        )
        path = args.output_dir / filename
        path.write_text(svg, encoding="utf-8")
        print(f"Wrote {path}")

    metadata = {
        "username": args.username,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "first_active_date": active[0].date.isoformat(),
        "last_date": days[-1].date.isoformat(),
        "active_days": len(active),
        "total_contributions": sum(day.count for day in days),
        "duration_seconds": args.duration,
        "ordering": "ascending-date",
        "growth_rule": "one segment per active contribution day",
    }
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
