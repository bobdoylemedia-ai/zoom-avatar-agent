"""Turn a spreadsheet into a document of pre-computed facts.

The knowledge system retrieves *text*, so a spreadsheet is close to useless to
it: tables get cut apart by chunking, header rows never travel with the rows
they describe, and anything that needs adding up would be added up by the model,
out loud, in a meeting, wrongly.

So the arithmetic happens here instead -- once, in Python, where it is correct --
and the output is prose the retriever can actually find. The agent never
calculates anything; it recites a sentence it was handed.

Two things shape the output:

* **Facts are written as questions and answers.** Retrieval matches a chunk
  against the *question someone asked*, so a chunk that already looks like that
  question is far more likely to come back.
* **Every fact is self-contained** -- it names the metric, the period and the
  unit -- because `rag.chunk_text` packs paragraphs up to ~900 characters and a
  fact that relies on a heading above it loses its meaning when the cut lands
  in the wrong place.

Usage:

    uv run python src/sheet_facts.py channel-analytics.csv
    uv run python src/sheet_facts.py channel-analytics.csv --out knowledge/channel.md

Then ingest the result like any other document:

    uv run python src/ingest.py channel knowledge/channel.md
"""

from __future__ import annotations

import argparse
import csv
import datetime
import pathlib
import re
import statistics
import sys

# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

# Columns whose values look like "7:50" are durations, not text.
_DURATION = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _to_number(value: str):
    """A float, an int, or None. Tolerates commas, currency and percent signs."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    text = text.lstrip("$£€").rstrip("%")
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _to_seconds(value: str):
    """"7:50" -> 470. Durations are stored as text in every export I've seen."""
    if not value or not _DURATION.match(str(value).strip()):
        return None
    parts = [int(p) for p in str(value).strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _to_date(value: str):
    # "%b %d, %Y" is what YouTube Studio exports ("Oct 4, 2024").
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(value).strip()[:19], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def load(path: pathlib.Path) -> tuple[list[dict], dict]:
    """Rows plus a map of column name -> role ("label", "date", "number", "duration").

    Roles are inferred from the data rather than declared, so the same utility
    survives a column being renamed or moved -- which every analytics export
    does eventually.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{path} has no rows")

    roles: dict[str, str] = {}
    for column in rows[0]:
        values = [r.get(column, "") for r in rows if str(r.get(column, "")).strip()]
        if not values:
            continue
        # A column of plain integers called "Duration" is seconds, not a count.
        # Left as a number it produces facts like "duration 951", which means
        # nothing said out loud.
        if "duration" in column.lower() and sum(
            _to_number(v) is not None for v in values
        ) > len(values) * 0.8:
            roles[column] = "duration"
        elif sum(_to_seconds(v) is not None for v in values) > len(values) * 0.8:
            roles[column] = "duration"
        elif sum(_to_number(v) is not None for v in values) > len(values) * 0.8:
            roles[column] = "number"
        elif sum(_to_date(v) is not None for v in values) > len(values) * 0.8:
            roles[column] = "date"
        else:
            roles[column] = "label"

    label_col = _pick_label(roles)
    parsed = []
    for row in rows:
        item: dict = {}
        for column, role in roles.items():
            raw = row.get(column, "")
            if role == "number":
                item[column] = _to_number(raw)
            elif role == "duration":
                # Either "7:50" or a plain count of seconds.
                item[column] = _to_seconds(raw)
                if item[column] is None:
                    item[column] = _to_number(raw)
            elif role == "date":
                item[column] = _to_date(raw)
            else:
                item[column] = str(raw).strip()
        # Exports often carry a summary row. Left in, it becomes a fact claiming
        # a video called "Total" out-performed everything else.
        name = str(item.get(label_col, "")).strip().lower() if label_col else ""
        if name in ("", "total", "totals", "grand total", "all"):
            continue
        parsed.append(item)
    if not parsed:
        raise SystemExit(f"{path} had rows but none with a usable label.")
    return parsed, roles


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def num(value) -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def secs(value) -> str:
    """Seconds as a clock time. Past an hour, say hours -- "7516:40" means nothing."""
    if value is None:
        return "not recorded"
    total = int(value)
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def pct(part, whole) -> str:
    if not whole:
        return "n/a"
    return f"{part / whole * 100:.1f}%"


def _is_rate(column: str) -> bool:
    """Rates must be averaged, never summed. Totalling a CTR column is nonsense."""
    lowered = column.lower()
    return "%" in lowered or "rate" in lowered or "average" in lowered


def avg_of(column: str) -> str:
    """"the average views", but "the average view duration" -- not "average average"."""
    lowered = column.lower()
    return lowered if lowered.startswith("average") else f"average {lowered}"


def plural(count: int, word: str = "entry") -> str:
    if word == "entry":
        return f"{count} {'entry' if count == 1 else 'entries'}"
    return f"{count} {word}{'' if count == 1 else 's'}"


def ask(*questions: str) -> str:
    """Several phrasings of the same question, above one answer.

    Retrieval scores a chunk on its similarity to the question as asked, and
    these facts are dangerously alike -- "which had the most subscribers per
    view" was beating "which had the highest views" for the question "which
    video got the most views". Carrying the words people actually use ("best",
    "worst", "top", "altogether") is what separates them.
    """
    return "\n".join(questions)


# --------------------------------------------------------------------------- #
# Topic tagging
# --------------------------------------------------------------------------- #

# Keyword buckets. Deliberately simple and visible: the alternative is asking a
# model to classify, which would be one more thing that can be quietly wrong.
TOPICS: dict[str, list[str]] = {
    "AI and tools": [
        "ai", "chatgpt", "claude", "avatar", "voice", "prompt", "automat",
        "agent", "notion", "no code", "fish audio", "code", "tool", "video tool",
        "second brain", "vision pro", "workflow",
    ],
    "personal development": [
        "habit", "brain", "confidence", "procrastinat", "morning", "goal",
        "change", "deep work", "listen", "read", "quit", "finish", "full-time",
        "development", "psychology", "routine", "to-do", "start",
    ],
}


def topic_of(title: str) -> str:
    lowered = title.lower()
    scores = {
        name: sum(1 for word in words if word in lowered)
        for name, words in TOPICS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else "other"


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #


def _pick_label(roles: dict) -> str | None:
    """The column to name things by.

    Prefer one that reads like a title. YouTube's export leads with a "Video"
    column of opaque ids (dQw4w9WgXcQ) and puts "Video title" after it, so
    taking simply the first text column would label every fact with an id.
    """
    labels = [c for c, r in roles.items() if r == "label"]
    if not labels:
        return None
    for want in ("title", "name", "subject", "item", "description"):
        for column in labels:
            if want in column.lower():
                return column
    return labels[0]


def build(rows: list[dict], roles: dict, source: str) -> str:
    label_col = _pick_label(roles)
    date_col = next((c for c, r in roles.items() if r == "date"), None)
    metric_cols = [c for c, r in roles.items() if r in ("number", "duration")]
    if not label_col or not metric_cols:
        raise SystemExit("Could not find a label column and at least one numeric column.")

    def value(row, column):
        return row.get(column)

    def total(column):
        vals = [v for v in (value(r, column) for r in rows) if v is not None]
        return sum(vals) if vals else None

    def mean(column):
        vals = [v for v in (value(r, column) for r in rows) if v is not None]
        return statistics.fmean(vals) if vals else None

    def median(column):
        vals = [v for v in (value(r, column) for r in rows) if v is not None]
        return statistics.median(vals) if vals else None

    out: list[str] = []
    add = out.append

    dates = sorted(d for d in (value(r, date_col) for r in rows) if d) if date_col else []
    span = f"{dates[0].isoformat()} to {dates[-1].isoformat()}" if dates else "an unstated period"

    # ---- preamble ---------------------------------------------------------
    add(f"# Facts calculated from {source}")
    add(
        "Every figure below was calculated directly from the source spreadsheet "
        "and is exact. Quote these numbers as they are written.\n\n"
        "Do not add, subtract, average or otherwise combine numbers from "
        "different facts to work out a new one. If someone asks for a figure "
        "that is not stated here, say you do not have that number rather than "
        "working it out."
    )
    add(
        f"Coverage: {len(rows)} entries covering {span}. "
        f"The metrics available are: {', '.join(metric_cols)}. "
        "Nothing outside that range or those metrics is included."
    )

    # ---- totals -----------------------------------------------------------
    add("## Totals and typical values")
    # "How many are there" is one of the most-asked questions and was only
    # answerable from the preamble, which is buried among instructions and
    # rarely retrieved. It needs to be a fact in its own right.
    add(
        ask(
            "How many entries does this cover?",
            "How many are there altogether?",
        )
        + f"\nThis covers {len(rows)} entries, spanning {span}."
    )
    for column in metric_cols:
        fmt = secs if roles[column] == "duration" else num
        if _is_rate(column) or roles[column] == "duration":
            low = column.lower()
            phrasings = [
                f"What is the {avg_of(column)}?",
                f"What is typical for {low}?",
                f"What does a typical entry get for {low}?",
                f"What is normal for {low}?",
            ]
            if roles[column] == "duration":
                # Nobody asks "what is the average duration" out loud.
                phrasings += [
                    f"How long is the average one?",
                    f"How long are they, typically?",
                    f"What is the usual length?",
                ]
            add(
                ask(*phrasings)
                + f"\nThe {avg_of(column)} across all {plural(len(rows))} is "
                f"{fmt(mean(column))}. The median is {fmt(median(column))}."
            )
        else:
            low = column.lower()
            add(
                ask(
                    f"What is the total {low}?",
                    f"How many {low} in total?",
                    f"How many {low} altogether, across everything?",
                    f"What is the overall {low} figure?",
                    f"What does a typical entry get for {low}?",
                    f"What is average or typical for {low}?",
                )
                + f"\nTotal {low} across all {plural(len(rows))} is "
                f"{fmt(total(column))}. The average per entry is {fmt(mean(column))} "
                f"and the median is {fmt(median(column))}."
            )

    add(
        "Why does the average differ from the median?\n"
        "The median is the middle value and the average is pulled upwards by a "
        "small number of unusually large entries. For a typical entry the median "
        "is the more honest figure."
    )

    # ---- rankings ---------------------------------------------------------
    add("## Best and worst")
    for column in metric_cols:
        ranked = [r for r in rows if value(r, column) is not None]
        if len(ranked) < 3:
            continue
        ranked.sort(key=lambda r: value(r, column), reverse=True)
        fmt = secs if roles[column] == "duration" else num
        top = ranked[:5]
        bottom = ranked[-3:]
        low = column.lower()
        listed = "; ".join(f"{r[label_col]} ({fmt(value(r, column))})" for r in top)
        add(
            ask(
                f"Which had the highest {low}?",
                f"Which one got the most {low}?",
                f"What was the best {low}?",
                f"Which performed best on {low}?",
                f"What is the top {low}?",
            )
            + f"\nThe highest {low} was {top[0][label_col]} at "
            f"{fmt(value(top[0], column))}. The top five by {low} were: {listed}."
        )
        worst = "; ".join(f"{r[label_col]} ({fmt(value(r, column))})" for r in reversed(bottom))
        add(
            ask(
                f"Which had the lowest {low}?",
                f"Which one got the least {low}?",
                f"What was the worst {low}?",
                f"Which performed worst on {low}?",
                f"Which was the weakest by {low}?",
            )
            + f"\nThe lowest {low} was {bottom[0][label_col]} at "
            f"{fmt(value(bottom[0], column))}. The three lowest were: {worst}."
        )

    # ---- efficiency ratios ------------------------------------------------
    # The interesting questions are rarely raw totals -- "most subscribers per
    # view" is a different video from "most views", and that is the surprise.
    views_col = next((c for c in metric_cols if c.lower() == "views"), None)
    if views_col:
        add("## Rates and efficiency")
        for column in metric_cols:
            if column == views_col or _is_rate(column) or roles[column] == "duration":
                continue
            scored = [
                (r, value(r, column) / value(r, views_col) * 1000)
                for r in rows
                if value(r, column) is not None and value(r, views_col)
            ]
            if len(scored) < 3:
                continue
            scored.sort(key=lambda pair: pair[1], reverse=True)
            best, best_rate = scored[0]
            worst, worst_rate = scored[-1]
            overall = total(column) / total(views_col) * 1000
            listed = "; ".join(f"{r[label_col]} ({rate:.1f})" for r, rate in scored[:3])
            add(
                f"Which had the most {column.lower()} per view?\n"
                f"Measured as {column.lower()} per 1,000 views, the best was "
                f"{best[label_col]} at {best_rate:.1f}, and the weakest was "
                f"{worst[label_col]} at {worst_rate:.1f}. The channel-wide figure is "
                f"{overall:.1f} per 1,000 views. Top three: {listed}. "
                f"Note this is a different ranking from raw {column.lower()}."
            )

    # ---- by month ---------------------------------------------------------
    if date_col and dates:
        add("## By month")
        months: dict[str, list[dict]] = {}
        for row in rows:
            when = value(row, date_col)
            if when:
                months.setdefault(when.strftime("%Y-%m"), []).append(row)
        for month in sorted(months):
            group = months[month]
            pretty = datetime.datetime.strptime(month, "%Y-%m").strftime("%B %Y")
            parts = []
            for column in metric_cols:
                vals = [v for v in (value(r, column) for r in group) if v is not None]
                if not vals:
                    continue
                fmt = secs if roles[column] == "duration" else num
                if _is_rate(column) or roles[column] == "duration":
                    parts.append(f"{avg_of(column)} {fmt(statistics.fmean(vals))}")
                else:
                    parts.append(f"{column.lower()} {fmt(sum(vals))}")
            add(
                ask(
                    f"How did {pretty} do?",
                    f"What were the numbers for {pretty}?",
                    f"How did we perform in {pretty}?",
                )
                + f"\nIn {pretty} there were {plural(len(group))}: "
                + ", ".join(parts)
                + "."
            )

    # ---- trend ------------------------------------------------------------
    if date_col and len(rows) >= 8:
        add("## Trends over time")
        ordered = sorted((r for r in rows if value(r, date_col)), key=lambda r: value(r, date_col))
        half = len(ordered) // 2
        first, second = ordered[:half], ordered[half:]
        for column in metric_cols:
            a = [v for v in (value(r, column) for r in first) if v is not None]
            b = [v for v in (value(r, column) for r in second) if v is not None]
            if not a or not b:
                continue
            fmt = secs if roles[column] == "duration" else num
            avg_a, avg_b = statistics.fmean(a), statistics.fmean(b)
            direction = "improved" if avg_b > avg_a else "declined" if avg_b < avg_a else "stayed level"
            change = abs(avg_b - avg_a) / avg_a * 100 if avg_a else 0
            add(
                f"Is {column.lower()} getting better or worse over time?\n"
                f"Comparing the first half of the period with the second, average "
                f"{column.lower()} {direction} from {fmt(avg_a)} to {fmt(avg_b)}, "
                f"a change of {change:.1f}%."
            )

    # ---- topics -----------------------------------------------------------
    tagged = [(r, topic_of(r[label_col])) for r in rows]
    groups: dict[str, list[dict]] = {}
    for row, name in tagged:
        groups.setdefault(name, []).append(row)
    # A bucket of one or two is noise, not a comparison -- averaging a single
    # entry and presenting it beside a group of nineteen invites exactly the
    # wrong conclusion.
    MIN_GROUP = 3
    compared = {n: g for n, g in groups.items() if len(g) >= MIN_GROUP}
    if len(compared) > 1:
        add("## By topic")
        skipped = sum(len(g) for n, g in groups.items() if n not in compared)
        note = (
            f" Excluded from these comparisons: {plural(skipped)} that did not "
            "fall clearly into any group."
            if skipped
            else ""
        )
        add(
            "How were entries grouped by topic?\n"
            "Each entry was grouped by keywords in its title: "
            + ", ".join(f"{name} ({plural(len(g))})" for name, g in sorted(compared.items()))
            + ". The grouping is approximate and based on title wording alone."
            + note
        )
        for column in metric_cols:
            lines = []
            for name, group in sorted(compared.items()):
                vals = [v for v in (value(r, column) for r in group) if v is not None]
                if not vals:
                    continue
                fmt = secs if roles[column] == "duration" else num
                lines.append(
                    f"{name}: {avg_of(column)} {fmt(statistics.fmean(vals))} "
                    f"across {plural(len(vals))}"
                )
            if len(lines) > 1:
                add(
                    f"Which topic performs better on {column.lower()}?\n"
                    f"By topic — " + "; ".join(lines) + "."
                )

    # ---- per entry --------------------------------------------------------
    # One fact per row swamps everything else once there are a few hundred rows:
    # at 484 videos these were 84% of the index, and the aggregate facts stopped
    # surfacing because six near-identical per-video chunks filled every result.
    # Keep the ones people actually ask about -- the biggest, and the recent --
    # and let the rest be covered by the rankings and rollups above.
    PER_ENTRY_CAP = 150
    detailed = rows
    if len(rows) > PER_ENTRY_CAP:
        headline = next((c for c in metric_cols if c.lower() == "views"), metric_cols[0])
        by_size = sorted(
            (r for r in rows if value(r, headline) is not None),
            key=lambda r: value(r, headline),
            reverse=True,
        )
        keep = list(by_size[: PER_ENTRY_CAP * 2 // 3])
        if date_col:
            recent = sorted(
                (r for r in rows if value(r, date_col)),
                key=lambda r: value(r, date_col),
                reverse=True,
            )
            for row in recent:
                if len(keep) >= PER_ENTRY_CAP:
                    break
                if row not in keep:
                    keep.append(row)
        detailed = keep

    add("## Individual entries")
    if len(detailed) < len(rows):
        add(
            "Which entries are listed individually?\n"
            f"Individual figures are listed for {len(detailed)} of the {len(rows)} "
            "entries: the highest performing, plus the most recent. If someone asks "
            "about one that is not listed, say you do not have its individual "
            "figures rather than estimating them."
        )
    for row in detailed:
        parts = []
        for column in metric_cols:
            v = value(row, column)
            if v is None:
                continue
            fmt = secs if roles[column] == "duration" else num
            parts.append(f"{column.lower()} {fmt(v)}")
        when = value(row, date_col) if date_col else None
        dated = f", published {when.isoformat()}" if when else ""
        add(
            f"How did \"{row[label_col]}\" do?\n"
            f"\"{row[label_col]}\"{dated}: " + ", ".join(parts) + "."
        )

    return "\n\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-compute facts from a spreadsheet so the avatar can quote them."
    )
    parser.add_argument("csv_file", help="The spreadsheet, as CSV.")
    parser.add_argument("--out", help="Where to write the markdown (default: alongside the CSV).")
    parser.add_argument(
        "--since",
        help="Ignore rows dated before this (YYYY-MM-DD). For leaving an older era "
        "of the channel out of the figures.",
    )
    parser.add_argument("--until", help="Ignore rows dated after this (YYYY-MM-DD).")
    parser.add_argument(
        "--drop",
        action="append",
        default=[],
        help="Leave a column out entirely. Repeatable. Use it for anything the "
        "avatar should not be able to say out loud, such as revenue.",
    )
    args = parser.parse_args(argv)

    path = pathlib.Path(args.csv_file)
    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 1

    rows, roles = load(path)

    for pattern in args.drop:
        gone = [c for c in list(roles) if pattern.lower() in c.lower()]
        for column in gone:
            roles.pop(column)
            for row in rows:
                row.pop(column, None)
        if gone:
            print(f"Dropped column(s): {', '.join(gone)}")

    date_col = next((c for c, r in roles.items() if r == "date"), None)
    if (args.since or args.until) and date_col:
        since = _to_date(args.since) if args.since else None
        until = _to_date(args.until) if args.until else None
        if args.since and not since:
            print(f"Could not read --since {args.since!r}; expected YYYY-MM-DD", file=sys.stderr)
            return 1
        if args.until and not until:
            print(f"Could not read --until {args.until!r}; expected YYYY-MM-DD", file=sys.stderr)
            return 1
        before = len(rows)
        rows = [
            r for r in rows
            if r.get(date_col)
            and (since is None or r[date_col] >= since)
            and (until is None or r[date_col] <= until)
        ]
        print(f"Date filter kept {len(rows)} of {before} rows.")
        if not rows:
            print("Nothing left after filtering.", file=sys.stderr)
            return 1

    document = build(rows, roles, path.name)

    out = pathlib.Path(args.out) if args.out else path.with_suffix(".facts.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")

    facts = document.count("\n\n") + 1
    print(f"Read {len(rows)} rows from {path.name}")
    print("Columns: " + ", ".join(f"{c} ({r})" for c, r in roles.items()))
    print(f"Wrote {facts} facts, {len(document):,} characters -> {out}")
    print(f"\nIngest it with:\n  uv run python src/ingest.py <name> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
