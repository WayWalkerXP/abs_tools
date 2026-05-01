import csv
import re
from collections import defaultdict
from pathlib import Path


# --- Configuration ---
INPUT_CSV = "audiobookshelf_inventory.csv"
OUTPUT_CSV = "missing_series_parts.csv"

SERIES_COLUMN = "series"
SERIES_NUMBER_COLUMN = "series_number"
AUTHOR_COLUMN = "author"

# If True, only output series that are missing parts.
# If False, output all grouped series, including those with no missing parts.
ONLY_OUTPUT_MISSING = True
# ---------------------


def clean_text(value):
    """Return stripped text, or an empty string if the value is missing."""
    if value is None:
        return ""

    return str(value).strip()


def is_blank_or_junk(value):
    """Return True if a value should be treated as blank/useless."""
    cleaned = clean_text(value)

    if not cleaned:
        return True

    return cleaned.upper() in {"N/A", "NA", "NONE", "NULL", "-"}


def clean_series_name(series_name):
    """Return a cleaned series name, or None if it should be ignored."""
    if is_blank_or_junk(series_name):
        return None

    return clean_text(series_name)


def clean_author_name(author_name):
    """Return a cleaned author name."""
    if is_blank_or_junk(author_name):
        return "Unknown"

    return clean_text(author_name)


def normalize_series_name(series_name):
    """
    Normalize a series name so minor naming differences group together.

    Examples:
        'A Harry Bosch Novel'
        'A Harry Bosch Novel, Book'

    both become approximately:
        'harrybosch'

    This is intentionally aggressive because the goal is to find inconsistent
    series naming.
    """
    name = clean_text(series_name).lower()

    # Replace common separators with spaces before phrase cleanup.
    name = name.replace("&", " and ")
    name = re.sub(r"[_\-:]+", " ", name)

    # Remove parenthetical notes.
    name = re.sub(r"\([^)]*\)", " ", name)

    # Remove common descriptive words/phrases that often create false variants.
    removable_phrases = [
        r"\ba\b",
        r"\ban\b",
        r"\bthe\b",
        r"\bbook\b",
        r"\bbooks\b",
        r"\bnovel\b",
        r"\bnovels\b",
        r"\bseries\b",
        r"\bsaga\b",
        r"\btrilogy\b",
        r"\bquartet\b",
        r"\bchronicles\b",
        r"\bcycle\b",
        r"\bvolume\b",
        r"\bvol\b",
        r"\bpart\b",
    ]

    for pattern in removable_phrases:
        name = re.sub(pattern, " ", name)

    # Remove punctuation and spaces.
    name = re.sub(r"[^a-z0-9]+", "", name)

    return name


def normalize_author_name(author_name):
    """
    Normalize author name for grouping.

    This prevents 'Michael Connelly' and 'Connelly, Michael' from being treated
    as obviously different where possible.
    """
    author = clean_author_name(author_name).lower()

    if "," in author:
        parts = [part.strip() for part in author.split(",", maxsplit=1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            author = f"{parts[1]} {parts[0]}"

    author = re.sub(r"[^a-z0-9]+", "", author)

    return author or "unknown"


def parse_series_number(series_number):
    """
    Convert a series number into an integer when appropriate.

    Examples:
        "1"   -> 1
        "01"  -> 1
        "1.0" -> 1
        "1.5" -> None

    Decimal entries like 1.5 are ignored for gap detection because they are
    often side stories/novellas and should not force the sequence to include
    every decimal value.
    """
    if is_blank_or_junk(series_number):
        return None

    value = clean_text(series_number)

    try:
        number = float(value)
    except ValueError:
        return None

    if not number.is_integer():
        return None

    if number < 1:
        return None

    return int(number)


def format_number_list(numbers):
    """Format a set/list of numbers as a semicolon-separated string."""
    return ";".join(str(number) for number in sorted(numbers))


def read_series_groups(input_file):
    """
    Read the inventory CSV and group books by normalized series + normalized author.

    Returns:
        dict keyed by (normalized_series, normalized_author)
    """
    input_path = Path(input_file)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path.resolve()}")

    groups = defaultdict(
        lambda: {
            "series_variants": defaultdict(set),
            "authors": set(),
            "numbers": set(),
        }
    )

    with input_path.open("r", newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile)

        if reader.fieldnames is None:
            raise ValueError("Input CSV appears to have no header row.")

        required_columns = {
            SERIES_COLUMN,
            SERIES_NUMBER_COLUMN,
            AUTHOR_COLUMN,
        }

        missing_columns = required_columns - set(reader.fieldnames)

        if missing_columns:
            missing_text = ", ".join(sorted(missing_columns))
            raise ValueError(f"Missing required column(s): {missing_text}")

        for row in reader:
            series_name = clean_series_name(row.get(SERIES_COLUMN))
            author_name = clean_author_name(row.get(AUTHOR_COLUMN))
            series_number = parse_series_number(row.get(SERIES_NUMBER_COLUMN))

            if series_name is None:
                continue

            if series_number is None:
                continue

            normalized_series = normalize_series_name(series_name)
            normalized_author = normalize_author_name(author_name)

            if not normalized_series:
                continue

            group_key = (normalized_series, normalized_author)

            groups[group_key]["series_variants"][series_name].add(series_number)
            groups[group_key]["authors"].add(author_name)
            groups[group_key]["numbers"].add(series_number)

    return groups


def build_report_rows(groups):
    """Build output rows for the missing-series-parts report."""
    report_rows = []

    for (_normalized_series, _normalized_author), group_data in groups.items():
        present_numbers = group_data["numbers"]

        if not present_numbers:
            continue

        max_number = max(present_numbers)

        missing_numbers = [
            number
            for number in range(1, max_number + 1)
            if number not in present_numbers
        ]

        if ONLY_OUTPUT_MISSING and not missing_numbers:
            continue

        series_variants = sorted(group_data["series_variants"])
        authors = sorted(group_data["authors"])

        parts_by_variant = []

        for variant in series_variants:
            variant_numbers = group_data["series_variants"][variant]
            parts_by_variant.append(
                f"{variant}: {format_number_list(variant_numbers)}"
            )

        report_rows.append(
            {
                "Series": series_variants[0],
                "Author": "; ".join(authors),
                "Missing Parts": ";".join(str(number) for number in missing_numbers),
                "Series Variants": " | ".join(series_variants),
                "Parts By Variant": " | ".join(parts_by_variant),
            }
        )

    report_rows.sort(
        key=lambda row: (
            row["Author"].lower(),
            row["Series"].lower(),
        )
    )

    return report_rows


def write_report_csv(report_rows, output_file):
    """Write the report rows to a CSV file."""
    output_path = Path(output_file)

    fieldnames = [
        "Series",
        "Author",
        "Missing Parts",
        "Series Variants",
        "Parts By Variant",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"Exported {len(report_rows)} rows to {output_path.resolve()}")


def main():
    try:
        groups = read_series_groups(INPUT_CSV)
        report_rows = build_report_rows(groups)
        write_report_csv(report_rows, OUTPUT_CSV)
    except (FileNotFoundError, ValueError, OSError) as error:
        print(f"Error: {error}")


if __name__ == "__main__":
    main()
