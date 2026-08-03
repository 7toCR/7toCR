#!/usr/bin/env python3
"""Generate a growing GitHub contribution snake as animated SVG.

The snake moves orthogonally across the contribution grid and repeatedly seeks
the nearest reachable active cell. Empty cells are only traversal space; eating
and body growth happen exclusively when the snake reaches a contribution.

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
from collections import deque
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
    "spark": "#F59E0B",
    "spark_soft": "#FDE68A",
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
    "spark": "#FBBF24",
    "spark_soft": "#FEF3C7",
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
class GridPoint:
    column: int
    row: int


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class EatEvent:
    day: ContributionDay
    grid_point: GridPoint
    step_index: int
    distance: float


@dataclass(frozen=True)
class Route:
    grid_points: tuple[GridPoint, ...]
    points: tuple[Point, ...]
    eat_events: tuple[EatEvent, ...]
    total_distance: float
    step_distance: float


@dataclass(frozen=True)
class TimelineEntry:
    step_index: int
    distance: float
    key_time: float
    active_eaten: int
    eaten_date: dt.date | None = None


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


def fetch_range(
    username: str,
    token: str,
    start_date: dt.date,
    end_date: dt.date,
) -> list[ContributionDay]:
    start = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
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
            if date < start_date or date > end_date:
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


def rolling_year_start(today: dt.date) -> dt.date:
    """Return the first day in an inclusive 365-day contribution window."""
    return today - dt.timedelta(days=364)


def fill_calendar_days(
    days: Iterable[ContributionDay],
    start_date: dt.date,
    end_date: dt.date,
) -> list[ContributionDay]:
    by_date = {day.date: day for day in days}
    cursor = start_date
    result: list[ContributionDay] = []
    while cursor <= end_date:
        result.append(by_date.get(cursor, ContributionDay(cursor, 0, "NONE")))
        cursor += dt.timedelta(days=1)
    return result


def load_github_days(username: str, token: str, max_years: int, today: dt.date) -> list[ContributionDay]:
    if max_years == 1:
        start_date = rolling_year_start(today)
        print(
            f"Fetching rolling contribution calendar from {start_date} to {today}...",
            file=sys.stderr,
        )
        fetched = fetch_range(username, token, start_date, today)
        return fill_calendar_days(fetched, start_date, today)

    years = [year for year in fetch_contribution_years(username, token) if year <= today.year]
    if not years:
        years = [today.year]
    if max_years > 0:
        years = years[-max_years:]

    start_date = dt.date(min(years), 1, 1)
    fetched: list[ContributionDay] = []
    for year in years:
        year_start = dt.date(year, 1, 1)
        year_end = min(today, dt.date(year, 12, 31))
        print(f"Fetching contribution calendar for {year}...", file=sys.stderr)
        fetched.extend(fetch_range(username, token, year_start, year_end))

    return fill_calendar_days(fetched, start_date, today)


def generate_demo_days(today: dt.date, max_years: int) -> list[ContributionDay]:
    """Create deterministic sample data for local preview and validation."""
    rng = random.Random(7)
    start = rolling_year_start(today) if max_years == 1 else dt.date(today.year - 1, 1, 1)
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


def first_sunday(date: dt.date) -> dt.date:
    days_since_sunday = (date.weekday() + 1) % 7
    return date - dt.timedelta(days=days_since_sunday)


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


def build_grid(days: Sequence[ContributionDay]) -> tuple[
    dict[dt.date, GridPoint], dict[GridPoint, ContributionDay]
]:
    """Map displayed calendar dates to their discrete week/weekday cells."""
    if not days:
        raise ValueError("No calendar days")

    grid_start = first_sunday(days[0].date)
    date_grid: dict[dt.date, GridPoint] = {}
    grid_days: dict[GridPoint, ContributionDay] = {}
    for day in days:
        point = GridPoint(
            column=(day.date - grid_start).days // 7,
            row=(day.date.weekday() + 1) % 7,
        )
        date_grid[day.date] = point
        grid_days[point] = day
    return date_grid, grid_days


def build_layout(days: Sequence[ContributionDay], cell: int, gap: int) -> tuple[
    dict[dt.date, Point], float, int, int, float, float
]:
    pitch = cell + gap
    title_height = 64
    label_left = 30
    right_padding = 24
    month_label_height = 18
    bottom_padding = 24
    grid_height = 7 * pitch - gap
    date_grid, _ = build_grid(days)
    max_week = max(point.column for point in date_grid.values())
    grid_width = (max_week + 1) * pitch - gap
    width = int(label_left + grid_width + right_padding)
    grid_top = title_height + month_label_height
    height = int(grid_top + grid_height + bottom_padding)

    coordinates = {
        date: Point(
            x=label_left + point.column * pitch + cell / 2,
            y=grid_top + point.row * pitch + cell / 2,
        )
        for date, point in date_grid.items()
    }
    return coordinates, grid_top, width, height, label_left, grid_width


GRID_DIRECTIONS = (
    GridPoint(0, -1),
    GridPoint(-1, 0),
    GridPoint(1, 0),
    GridPoint(0, 1),
)


def grid_neighbors(point: GridPoint) -> Iterable[GridPoint]:
    for direction in GRID_DIRECTIONS:
        yield GridPoint(point.column + direction.column, point.row + direction.row)


def manhattan_distance(left: GridPoint, right: GridPoint) -> int:
    return abs(left.column - right.column) + abs(left.row - right.row)


def choose_start_cell(
    active_days: Sequence[ContributionDay],
    date_grid: dict[dt.date, GridPoint],
    grid_days: dict[GridPoint, ContributionDay],
) -> GridPoint:
    """Choose a stable nearby empty cell, preferring the oldest food's left side."""
    oldest = min(active_days, key=lambda day: day.date)
    target = date_grid[oldest.date]
    empty_cells = [point for point, day in grid_days.items() if not day.active]
    if not empty_cells:
        return target

    def candidate_key(point: GridPoint) -> tuple[int, int, dt.date, int, int]:
        distance = manhattan_distance(point, target)
        if point.row == target.row and point.column < target.column:
            direction_rank = 0
        elif point.column < target.column:
            direction_rank = 1
        elif point.row == target.row:
            direction_rank = 2
        else:
            direction_rank = 3
        return (
            distance,
            direction_rank,
            grid_days[point].date,
            point.column,
            point.row,
        )

    return min(empty_cells, key=candidate_key)


def shortest_paths(
    start: GridPoint,
    legal_cells: set[GridPoint],
) -> tuple[dict[GridPoint, int], dict[GridPoint, GridPoint]]:
    """Run deterministic four-direction BFS over displayed contribution cells."""
    distances = {start: 0}
    parents: dict[GridPoint, GridPoint] = {}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in grid_neighbors(current):
            if neighbor not in legal_cells or neighbor in distances:
                continue
            distances[neighbor] = distances[current] + 1
            parents[neighbor] = current
            queue.append(neighbor)
    return distances, parents


def reconstruct_path(
    start: GridPoint,
    target: GridPoint,
    parents: dict[GridPoint, GridPoint],
) -> list[GridPoint]:
    if target == start:
        return [start]
    path = [target]
    while path[-1] != start:
        path.append(parents[path[-1]])
    path.reverse()
    return path


def build_route(
    days: Sequence[ContributionDay],
    coordinates: dict[dt.date, Point],
) -> Route:
    """Build a deterministic nearest-food route over contribution-grid cells."""
    active_days = [day for day in days if day.active]
    if not active_days:
        raise ValueError("No active contribution days")

    date_grid, grid_days = build_grid(days)
    point_coordinates = {
        date_grid[date]: point
        for date, point in coordinates.items()
    }
    legal_cells = set(grid_days)
    current = choose_start_cell(active_days, date_grid, grid_days)
    route_grid = [current]
    remaining = {date_grid[day.date]: day for day in active_days}
    event_data: list[tuple[ContributionDay, GridPoint, int]] = []
    first_target = True

    while remaining:
        distances, parents = shortest_paths(current, legal_cells)
        reachable = [
            (point, day)
            for point, day in remaining.items()
            if point in distances
        ]
        if not reachable:
            raise ValueError("An active contribution cell is unreachable")

        target_distances = (
            {point: manhattan_distance(point, current) for point in remaining}
            if first_target
            else distances
        )
        target, target_day = min(
            reachable,
            key=lambda item: (
                target_distances[item[0]],
                item[1].date,
                item[0].column,
                item[0].row,
            ),
        )
        path = reconstruct_path(current, target, parents)
        route_grid.extend(path[1:])
        event_data.append((target_day, target, len(route_grid) - 1))
        del remaining[target]
        current = target
        first_target = False

    route_points = tuple(point_coordinates[point] for point in route_grid)
    total_distance, cumulative = polyline_length(route_points)
    step_distance = cumulative[1] if len(cumulative) > 1 else 0.0
    events = tuple(
        EatEvent(
            day=day,
            grid_point=point,
            step_index=step_index,
            distance=cumulative[step_index],
        )
        for day, point, step_index in event_data
    )
    return Route(
        grid_points=tuple(route_grid),
        points=route_points,
        eat_events=events,
        total_distance=total_distance,
        step_distance=step_distance,
    )


def build_timeline(
    route: Route,
    duration: float,
    start_hold: float,
    end_hold: float,
    active_slowdown: float,
) -> list[TimelineEntry]:
    """Allocate animation time to real grid steps and short food-arrival pauses."""
    if duration <= start_hold + end_hold:
        raise ValueError("duration must exceed start_hold + end_hold")
    if active_slowdown <= 0:
        raise ValueError("active_slowdown must be positive")

    movement_duration = duration - start_hold - end_hold
    event_by_step = {event.step_index: event for event in route.eat_events}
    event_count = len(route.eat_events)
    total_weight = route.total_distance + active_slowdown * event_count
    weighted_pause = (
        movement_duration * active_slowdown * event_count / total_weight
        if total_weight > 0
        else 0.0
    )
    pause_duration = min(weighted_pause, 0.24 * event_count, movement_duration * 0.45)
    pause_per_event = pause_duration / event_count if event_count else 0.0
    seconds_per_distance = (
        (movement_duration - pause_duration) / route.total_distance
        if route.total_distance > 0
        else 0.0
    )

    entries = [TimelineEntry(0, 0.0, 0.0, 0)]
    active_eaten = 0
    elapsed = start_hold
    start_event = event_by_step.get(0)
    if start_event is not None:
        active_eaten += 1
    entries.append(
        TimelineEntry(
            step_index=0,
            distance=0.0,
            key_time=elapsed / duration,
            active_eaten=active_eaten,
            eaten_date=start_event.day.date if start_event else None,
        )
    )
    if start_event is not None:
        elapsed += pause_per_event
        entries.append(TimelineEntry(0, 0.0, elapsed / duration, active_eaten))

    for step_index in range(1, len(route.points)):
        distance = step_index * route.step_distance
        elapsed += route.step_distance * seconds_per_distance
        event = event_by_step.get(step_index)
        if event is not None:
            active_eaten += 1
        entries.append(
            TimelineEntry(
                step_index=step_index,
                distance=distance,
                key_time=elapsed / duration,
                active_eaten=active_eaten,
                eaten_date=event.day.date if event else None,
            )
        )
        if event is not None:
            elapsed += pause_per_event
            entries.append(
                TimelineEntry(step_index, distance, elapsed / duration, active_eaten)
            )

    entries.append(
        TimelineEntry(
            step_index=len(route.points) - 1,
            distance=route.total_distance,
            key_time=1.0,
            active_eaten=active_eaten,
        )
    )
    return entries


def month_labels(
    days: Sequence[ContributionDay],
    coordinates: dict[dt.date, Point],
) -> list[tuple[str, float]]:
    labels: list[tuple[str, float]] = []
    previous_month: tuple[int, int] | None = None
    for day in days:
        month = (day.date.year, day.date.month)
        if month == previous_month:
            continue
        label = day.date.strftime("%b")
        if day.date.month == 1:
            label = f"{label} '{str(day.date.year)[2:]}"
        labels.append((label, coordinates[day.date].x - 4))
        previous_month = month
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

    coordinates, grid_top, width, height, label_left, grid_width = build_layout(days, cell, gap)
    route = build_route(days, coordinates)
    timeline = build_timeline(
        route,
        duration=duration,
        start_hold=1.2,
        end_hold=1.6,
        active_slowdown=20.0,
    )
    total_path_length = max(route.total_distance, 0.01)
    path_d = svg_path(route.points)
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

    active_eat_times = {
        entry.eaten_date: entry.key_time
        for entry in timeline
        if entry.eaten_date is not None
    }

    total_contributions = sum(day.count for day in days)
    total_active_days = len(active_days)

    out: list[str] = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(username)} nearest-food contribution snake">'
    )
    out.append("<title>Nearest-food GitHub contribution snake</title>")
    out.append(
        f"<desc>Across the 365-day window from {days[0].date.isoformat()} to "
        f"{days[-1].date.isoformat()}, the snake follows four-direction shortest "
        f"paths to eat {total_active_days} active contribution days; empty cells "
        "are traversal space only.</desc>"
    )
    out.append("<defs>")
    out.append(
        f'<filter id="snake-shadow" x="-30%" y="-30%" width="160%" height="160%">'
        f'<feDropShadow dx="0" dy="1.5" stdDeviation="1.4" flood-color="{palette["snake_outline"]}" flood-opacity="0.45"/>'
        "</filter>"
    )
    out.append(
        '<filter id="eat-glow" x="-120%" y="-120%" width="340%" height="340%">'
        f'<feGaussianBlur stdDeviation="2.1" result="blur"/>'
        f'<feFlood flood-color="{palette["spark"]}" flood-opacity="0.9" result="color"/>'
        '<feComposite in="color" in2="blur" operator="in" result="glow"/>'
        '<feMerge><feMergeNode in="glow"/><feMergeNode in="SourceGraphic"/></feMerge>'
        "</filter>"
    )
    out.append("</defs>")
    out.append(f'<rect width="100%" height="100%" rx="12" fill="{palette["background"]}"/>')

    out.append(
        f'<text x="18" y="26" fill="{palette["text"]}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        f'font-size="16" font-weight="600">{escape(username)} · Nearest-Food Contribution Snake</text>'
    )
    out.append(
        f'<text x="18" y="47" fill="{palette["muted"]}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        f'font-size="11">last 365 days · {total_active_days} active days · {total_contributions} contributions</text>'
    )

    # One rolling-year calendar with compact month markers.
    for label, x in month_labels(days, coordinates):
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
            anticipation = max(0.0, eat_time - 0.004)
            after = min(1.0, eat_time + 0.003)
            out.append(
                f'<animate attributeName="fill" dur="{fmt(duration)}s" repeatCount="indefinite" '
                f'values="{original_fill};{original_fill};{palette["spark_soft"]};{palette["empty"]};{palette["empty"]}" '
                f'keyTimes="0;{fmt(anticipation)};{fmt(eat_time)};{fmt(after)};1" calcMode="discrete"/>'
            )
            out.append(
                f'<animate attributeName="opacity" dur="{fmt(duration)}s" repeatCount="indefinite" '
                f'values="1;1;0.35;1;1" keyTimes="0;{fmt(anticipation)};{fmt(eat_time)};{fmt(after)};1"/>'
            )
        out.append("</rect>")

    # A short pixel-burst marks each bite without obscuring neighboring cells.
    burst_offsets = ((-7, 0), (7, 0), (0, -7), (0, 7))
    for day in active_days:
        eat_time = active_eat_times.get(day.date)
        if eat_time is None:
            continue
        point = coordinates[day.date]
        burst_start = max(0.0, eat_time - 0.001)
        burst_peak = min(1.0, eat_time + 0.003)
        burst_end = min(1.0, eat_time + 0.009)
        out.append(
            f'<g transform="translate({fmt(point.x)} {fmt(point.y)})" '
            'pointer-events="none" filter="url(#eat-glow)">'
        )
        out.append(
            f'<circle cx="0" cy="0" r="2.4" fill="none" stroke="{palette["spark"]}" stroke-width="1.4">'
            f'<animate attributeName="r" dur="{fmt(duration)}s" repeatCount="indefinite" '
            f'values="2.4;2.4;7.5;10" keyTimes="0;{fmt(burst_start)};{fmt(burst_peak)};{fmt(burst_end)};1"/>'
            f'<animate attributeName="opacity" dur="{fmt(duration)}s" repeatCount="indefinite" '
            f'values="0;0;0.95;0;0" keyTimes="0;{fmt(burst_start)};{fmt(burst_peak)};{fmt(burst_end)};1"/>'
            '</circle>'
        )
        for dx, dy in burst_offsets:
            out.append(
                f'<rect x="-1.25" y="-1.25" width="2.5" height="2.5" rx="0.6" fill="{palette["spark_soft"]}">'
                f'<animateTransform attributeName="transform" type="translate" dur="{fmt(duration)}s" repeatCount="indefinite" '
                f'values="0 0;0 0;{dx} {dy};{fmt(dx * 1.45)} {fmt(dy * 1.45)}" '
                f'keyTimes="0;{fmt(burst_start)};{fmt(burst_peak)};{fmt(burst_end)};1"/>'
                f'<animate attributeName="opacity" dur="{fmt(duration)}s" repeatCount="indefinite" '
                f'values="0;0;1;0;0" keyTimes="0;{fmt(burst_start)};{fmt(burst_peak)};{fmt(burst_end)};1"/>'
                '</rect>'
            )
        out.append("</g>")

    # The animated body is the only prominent route visual.
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
    parser.add_argument("--duration", type=float, default=48.0, help="Animation duration in seconds")
    parser.add_argument("--max-years", type=int, default=1, help="1 means the latest rolling 365 days")
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
        days = generate_demo_days(args.today, args.max_years)
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
        "window_days": len(days),
        "window_start": days[0].date.isoformat(),
        "ordering": "nearest-reachable",
        "route": "nearest-food-grid",
        "growth_rule": "one segment per active contribution day",
    }
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
