import csv
import os
import re
from pathlib import Path

import requests


# --- Configuration ---
ABS_SERVER_URL = ""
API_KEY = os.getenv("ABS_KEY")
OUTPUT_CSV = "audiobookshelf_inventory.csv"
REQUEST_TIMEOUT = 30
# ---------------------


def format_duration(seconds):
    """Convert seconds into HH:MM format."""
    if not seconds:
        return "00:00"

    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "00:00"

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)

    return f"{hours:02d}:{minutes:02d}"


def split_series_and_sequence(series_value):
    """
    Split Audiobookshelf seriesName values like:

        'Arcane Ascension #1'
        'The Horus Heresy #14'
        'Hell Divers #1.5'

    into:

        ('Arcane Ascension', '1')
        ('The Horus Heresy', '14')
        ('Hell Divers', '1.5')

    If no trailing sequence is found, return the full value as the series name.
    """
    if not series_value:
        return "N/A", "-"

    series_value = str(series_value).strip()

    match = re.match(r"^(.*?)\s+#([\d.]+)\s*$", series_value)

    if match:
        series_name = match.group(1).strip()
        sequence = match.group(2).strip()
        return series_name, sequence

    return series_value, "-"


def estimate_bitrate_kbps(size_bytes, duration_seconds):
    """
    Estimate average bitrate in kbps using:

        bitrate = size_bytes * 8 / duration_seconds

    This is estimated from the item size and duration because the library items
    endpoint does not appear to return embedded audio stream metadata.
    """
    try:
        size_bytes = float(size_bytes)
        duration_seconds = float(duration_seconds)
    except (TypeError, ValueError):
        return "N/A"

    if size_bytes <= 0 or duration_seconds <= 0:
        return "N/A"

    bitrate_kbps = (size_bytes * 8) / duration_seconds / 1000

    return f"{bitrate_kbps:.1f}k"


def get_json(url, headers):
    """
    Make a GET request and return JSON data.

    Raises:
        requests.RequestException: for connection or HTTP errors.
        ValueError: if the response is not valid JSON.
    """
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_audiobook_data():
    """Retrieve audiobook data from Audiobookshelf and return rows for CSV export."""
    if not API_KEY:
        print("Error: ABS_KEY environment variable is not set.")
        return []

    headers = {"Authorization": f"Bearer {API_KEY}"}

    libraries_url = f"{ABS_SERVER_URL}/api/libraries"

    try:
        library_data = get_json(libraries_url, headers)
    except requests.RequestException as error:
        print(f"Error connecting to Audiobookshelf server: {error}")
        return []
    except ValueError as error:
        print(f"Error parsing library response as JSON: {error}")
        return []

    libraries = library_data.get("libraries", [])
    audiobook_rows = []

    for library in libraries:
        if library.get("mediaType") != "book":
            continue

        library_id = library.get("id")
        library_name = library.get("name", "Unknown Library")

        if not library_id:
            print(f"Skipping library with missing ID: {library_name}")
            continue

        items_url = f"{ABS_SERVER_URL}/api/libraries/{library_id}/items?limit=0"

        try:
            items_data = get_json(items_url, headers)
        except requests.RequestException as error:
            print(f"Error retrieving items from library '{library_name}': {error}")
            continue
        except ValueError as error:
            print(f"Error parsing items response for library '{library_name}' as JSON: {error}")
            continue

        items = items_data.get("results", [])

        for item in items:
            media = item.get("media", {})
            metadata = media.get("metadata", {})

            title = metadata.get("title") or "Unknown"
            subtitle = metadata.get("subtitle") or ""
            author = metadata.get("authorName") or "Unknown"
            author_lf = metadata.get("authorNameLF") or ""
            narrator = metadata.get("narratorName") or ""
            asin = metadata.get("asin") or ""
            isbn = metadata.get("isbn") or ""
            language = metadata.get("language") or ""
            published_year = metadata.get("publishedYear") or ""
            published_date = metadata.get("publishedDate") or ""
            publisher = metadata.get("publisher") or ""

            genres = metadata.get("genres") or []
            if isinstance(genres, list):
                genres_text = "; ".join(str(genre) for genre in genres)
            else:
                genres_text = str(genres)

            raw_series = metadata.get("seriesName")
            series, series_num = split_series_and_sequence(raw_series)

            duration_raw = media.get("duration", 0)
            duration_formatted = format_duration(duration_raw)

            size_bytes = media.get("size", 0)
            estimated_bitrate = estimate_bitrate_kbps(size_bytes, duration_raw)

            # The library items endpoint does not appear to return channel data.
            mode = "N/A"

            full_path = (
                item.get("path")
                or item.get("relPath")
                or "Unknown"
            )

            audiobook_rows.append(
                {
                    "library": library_name,
                    "title": title,
                    "subtitle": subtitle,
                    "series": series,
                    "series_number": series_num,
                    "raw_series_name": raw_series or "",
                    "author": author,
                    "author_last_first": author_lf,
                    "narrator": narrator,
                    "length_hhmm": duration_formatted,
                    "duration_seconds": duration_raw or 0,
                    "size_bytes": size_bytes or 0,
                    "estimated_bitrate": estimated_bitrate,
                    "mode": mode,
                    "asin": asin,
                    "isbn": isbn,
                    "language": language,
                    "published_year": published_year,
                    "published_date": published_date,
                    "publisher": publisher,
                    "genres": genres_text,
                    "full_path": full_path,
                }
            )

    return audiobook_rows


def write_csv(rows, output_file):
    """Write audiobook rows to a CSV file."""
    if not rows:
        print("No audiobook data found. CSV file was not created.")
        return

    output_path = Path(output_file)

    fieldnames = [
        "library",
        "title",
        "subtitle",
        "series",
        "series_number",
        "raw_series_name",
        "author",
        "author_last_first",
        "narrator",
        "length_hhmm",
        "duration_seconds",
        "size_bytes",
        "estimated_bitrate",
        "mode",
        "asin",
        "isbn",
        "language",
        "published_year",
        "published_date",
        "publisher",
        "genres",
        "full_path",
    ]

    try:
        with output_path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=fieldnames,
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            writer.writerows(rows)
    except OSError as error:
        print(f"Error writing CSV file: {error}")
        return

    print(f"Exported {len(rows)} audiobook records to {output_path.resolve()}")


def main():
    audiobook_rows = get_audiobook_data()
    write_csv(audiobook_rows, OUTPUT_CSV)


if __name__ == "__main__":
    main()
