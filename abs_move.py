#!/usr/bin/env python3
"""
ABS (Audiobook Sorter) — Python 3.13+ file mover for audiobooks

- Reads metadata from M4B/M4A/MP3 files in a directory.
- Uses two path templates (with/without series) from YAML config at ~/.config/abs/config.yml
- Moves files into the rendered destination path.
- --dryrun writes a CSV report (no moves) with columns:
    Album, Artist, Narrator, Year, Series, Series-Part, Filename
- --duplicates controls what happens when the destination file already exists:
    ask    (default): Show both files' title, author, bitrate, chapter count, and size, then ask.
    force:           Overwrite the existing destination file.
    skip:            Leave both files where they are.
    delete:          Delete the new/source file and keep the existing destination file.
- --replace_spaces STRING:
    Replaces spaces in *series* and *title* (and optionally author) with STRING.
    Use "no_spaces" to remove spaces entirely.
- --replace_author_spaces:
    Also apply the space replacement rule to the author folder name.

Dependencies: mutagen, pyyaml

    pip install mutagen pyyaml
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Literal

import yaml
from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

SUPPORTED_EXTS = {".m4b", ".m4a", ".mp3"}
CONFIG_PATH = Path.home() / ".config/abs/config.yml"

DuplicatePolicy = Literal["ask", "force", "skip", "delete"]


# ----------------- helpers ----------------- #

def sanitize(name: str) -> str:
    """Sanitize a string for filesystem use (Windows-safe)."""
    if not name:
        return ""
    # Replace forbidden characters with underscore
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    # Strip trailing spaces/dots
    name = re.sub(r"[ .]+$", "", name)
    return name.strip()


def apply_space_repl(text: str, repl: str) -> str:
    """Apply space replacement (or removal) to a string."""
    if text is None:
        return ""
    if repl == "":
        # "no_spaces" → "" already handled above; just remove spaces
        return text.replace(" ", "")
    return text.replace(" ", repl)


def load_config(path: Path) -> Dict[str, Any]:
    """Load YAML config (templates + options), with safe defaults."""
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    cfg.setdefault("templates", {})
    cfg["templates"].setdefault(
        "with_series",
        "Audiobooks/{author}/{series}/({series_part}) {title} ({year})/{title} ({year}).{ext}",
    )
    cfg["templates"].setdefault(
        "without_series",
        "Audiobooks/{author}/{title} ({year})/{title} ({year}).{ext}",
    )

    cfg.setdefault("options", {})
    cfg["options"].setdefault("recursive", False)
    return cfg


@dataclass
class TrackMeta:
    album: str = ""
    artist: str = ""
    albumartist: str = ""
    title: str = ""
    narrator: str = ""
    year: str = ""
    date: str = ""
    series: str = ""
    series_part: str = ""

    @property
    def author(self) -> str:
        return self.albumartist or self.artist

    @property
    def year_best(self) -> str:
        if self.year and re.match(r"^\d{4}$", self.year):
            return self.year
        if self.date:
            m = re.search(r"(\d{4})", self.date)
            if m:
                return m.group(1)
        return ""

    @property
    def title_best(self) -> str:
        return self.title or self.album


@dataclass
class FileSummary:
    path: Path
    title: str = ""
    author: str = ""
    run_time: str = ""
    bitrate: str = "Unknown"
    filesize: str = "Unknown"
    chapters: str = "Unknown"


def get_first_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v)


def join_list(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(map(str, v))
    return str(v)


def format_series_part(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "00"
    # Pure integer → 2-digit
    if re.fullmatch(r"\d+", raw):
        return f"{int(raw):02d}"
    # Decimal like 1.5 → 01.5
    m = re.fullmatch(r"(\d+)\.(\d+)", raw)
    if m:
        int_part, frac = m.groups()
        return f"{int(int_part):02d}.{frac}"

    # Fallback: return as-is
    return raw


def extract_meta(path: Path) -> TrackMeta:
    """Extract metadata from MP4/MP3/other tags using mutagen."""
    audio = MutagenFile(path)
    tm = TrackMeta()
    if audio is None:
        return tm

    try:
        # ---------- MP4 / M4B ----------
        if isinstance(audio, MP4):
            tags = audio.tags or {}

            tm.title = get_first_str(tags.get("©nam"))
            tm.album = get_first_str(tags.get("©alb"))
            tm.artist = get_first_str(tags.get("©ART"))
            tm.albumartist = get_first_str(tags.get("aART"))
            tm.date = get_first_str(tags.get("©day"))
            m = re.search(r"(\d{4})", tm.date or "")
            tm.year = m.group(1) if m else ""

            # Narrator from composer/writer if present
            tm.narrator = join_list(tags.get("©com") or tags.get("©wrt"))

            # Freeform iTunes atoms: ----:com.apple.iTunes:KEY
            freeforms: Dict[str, str] = {}
            for k, v in (tags or {}).items():
                if isinstance(k, str) and k.startswith("----:com.apple.iTunes:"):
                    suffix = k.split(":", 2)[-1].strip().lower()
                    val = v[0] if isinstance(v, list) and v else v
                    if isinstance(val, (bytes, bytearray)):
                        try:
                            val = val.decode("utf-8", errors="ignore")
                        except Exception:
                            val = str(val)
                    freeforms[suffix] = str(val) if val is not None else ""

            # Also check plain keys case-insensitively
            plain_ci: Dict[str, Any] = {str(k).lower(): v for k, v in (tags or {}).items()}

            series_val = (
                freeforms.get("series")
                or get_first_str(plain_ci.get("series"))
                or ""
            )
            series_part_val = (
                freeforms.get("series-part")
                or freeforms.get("series_part")
                or get_first_str(plain_ci.get("series-part"))
                or get_first_str(plain_ci.get("series_part"))
                or ""
            )

            tm.series = series_val.strip()
            tm.series_part = format_series_part(series_part_val.strip())

        # ---------- MP3 / ID3 ----------
        elif isinstance(audio.tags, ID3):
            tags: ID3 = audio.tags

            tm.title = get_first_str(tags.get("TIT2"))
            tm.album = get_first_str(tags.get("TALB"))
            tm.artist = get_first_str(tags.get("TPE1"))
            tm.albumartist = get_first_str(tags.get("TPE2"))
            tm.narrator = join_list(tags.get("TCOM"))

            y = get_first_str(tags.get("TYER"))
            d = get_first_str(tags.get("TDRC"))
            if y:
                tm.year = re.sub(r"[^0-9]", "", y)[:4]
            else:
                m = re.search(r"(\d{4})", d or "")
                tm.year = m.group(1) if m else ""
            tm.date = d

            # Custom TXXX frames for series info
            txxx_all = tags.getall("TXXX")
            series_val = ""
            series_part_val = ""
            for f in txxx_all:
                desc = getattr(f, "desc", "").lower()
                text = join_list(getattr(f, "text", []))
                if "series" in desc and "part" not in desc:
                    series_val = text
                if "series" in desc and "part" in desc:
                    series_part_val = text

            tm.series = series_val.strip()
            tm.series_part = format_series_part(series_part_val.strip())

        # ---------- Other tag types (Vorbis/APE/etc.) ----------
        else:
            tags: Dict[str, Any] = {str(k).lower(): v for k, v in (audio.tags or {}).items()}

            def get_ci(*keys: str) -> str:
                for k in keys:
                    v = tags.get(k.lower())
                    if v is not None:
                        return get_first_str(v)
                return ""

            tm.title = get_ci("title")
            tm.album = get_ci("album")
            tm.artist = get_ci("artist")
            tm.albumartist = get_ci("albumartist", "album artist")
            tm.narrator = join_list(tags.get("composer")) or get_ci("composer")
            tm.year = re.sub(r"[^0-9]", "", get_ci("year"))[:4]
            tm.date = get_ci("date")
            tm.series = get_ci("series")
            tm.series_part = format_series_part(get_ci("series-part", "series_part"))

    except Exception:
        # On any tag weirdness, just fall back to whatever we already have.
        pass

    return tm


def get_audio_bitrate(path: Path) -> str:
    """Return a human-readable bitrate string, when mutagen can detect it."""
    try:
        audio = MutagenFile(path)
        bitrate = getattr(getattr(audio, "info", None), "bitrate", None)
        if bitrate is None:
            return "Unknown"

        kbps = int(round(bitrate / 1000))
        return f"{kbps} kbps"
    except Exception:
        return "Unknown"



def get_chapter_count(path: Path) -> str:
    """Return the number of embedded chapters mutagen can detect.

    MP4/M4B files usually expose chapters through audio.chapters when the
    moov.udta.chpl atom is present. MP3 files may expose ID3 CHAP frames.
    Other formats vary, so return Unknown when the structure is not supported
    or cannot be read.
    """
    try:
        audio = MutagenFile(path)
        if audio is None:
            return "Unknown"

        chapters = getattr(audio, "chapters", None)
        if chapters is not None:
            return str(len(chapters))

        tags = getattr(audio, "tags", None)
        if isinstance(tags, ID3):
            return str(len(tags.getall("CHAP")))

        return "Unknown"
    except Exception:
        return "Unknown"

def get_audio_length(path: Path) -> str:
    """
    Return the audio file runtime as HH:MM:SS.
    Mutagen stores audio length in seconds as a float at:
        audio.info.length
    If the length cannot be read, return "00:00:00".
    """
    audio = MutagenFile(path)

    if audio is None:
        return "00:00:00"
    if not hasattr(audio, "info") or audio.info is None:
        return "00:00:00"
    length_seconds = getattr(audio.info, "length", None)
    if length_seconds is None:
        return "00:00:00"

    total_seconds = int(round(length_seconds))

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def format_file_size(path: Path) -> str:
    """Return a human-readable file size."""
    try:
        size = path.stat().st_size
    except OSError:
        return "Unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{size} B"


def summarize_file(path: Path, fallback_meta: TrackMeta | None = None) -> FileSummary:
    """
    Build the title/author/bitrate/filesize summary used during duplicate review.

    fallback_meta is useful for the source file because main() has already read it.
    For the existing destination file, metadata is read directly from that file.
    """
    meta = fallback_meta if fallback_meta is not None else extract_meta(path)

    return FileSummary(
        path=path,
        title=meta.title_best or path.stem,
        author=meta.author or "Unknown",
        run_time=get_audio_length(path),
        bitrate=get_audio_bitrate(path),
        filesize=format_file_size(path),
        chapters=get_chapter_count(path),
    )


def print_duplicate_summary(existing: FileSummary, new: FileSummary) -> None:
    """Display a side-by-side-ish duplicate summary."""
    print("\nDuplicate destination found:")
    print(f"  Existing: {existing.path}")
    print(f"  New:      {new.path}")
    print()
    print("Existing file:")
    print(f"  Title:    {existing.title}")
    print(f"  Author:   {existing.author}")
    print(f"  Length:   {existing.run_time}")
    print(f"  Bitrate:  {existing.bitrate}")
    print(f"  Chapters: {existing.chapters}")
    print(f"  Filesize: {existing.filesize}")
    print()
    print("New file:")
    print(f"  Title:    {new.title}")
    print(f"  Author:   {new.author}")
    print(f"  Length:   {new.run_time}")
    print(f"  Bitrate:  {new.bitrate}")
    print(f"  Chapters: {new.chapters}")
    print(f"  Filesize: {new.filesize}")


def ask_duplicate_action(existing: Path, new: Path, new_meta: TrackMeta) -> DuplicatePolicy:
    """Ask the user what to do with a duplicate destination."""
    existing_summary = summarize_file(existing)
    new_summary = summarize_file(new, fallback_meta=new_meta)

    print_duplicate_summary(existing_summary, new_summary)

    valid_actions: dict[str, DuplicatePolicy] = {
        "o": "force",
        "overwrite": "force",
        "d": "delete",
        "delete": "delete",
        "s": "skip",
        "skip": "skip",
    }

    while True:
        answer = input(
            "\nAction? [O]verwrite existing / [D]elete new file / [S]kip: "
        ).strip().lower()

        if answer in valid_actions:
            return valid_actions[answer]

        print("Please enter O, D, or S. The machine is patient. I am pretending to be.")


def confirm_delete_mode() -> None:
    """
    Confirm startup use of --duplicates delete.

    This mode deletes source files when duplicates are found. That is useful, but it
    is also the option most likely to make a future version of you say words that
    would alarm nearby clergy.
    """
    print()
    print("WARNING: --duplicates delete is destructive.")
    print("When a destination duplicate is found, the file being moved will be DELETED.")
    print("The existing file on the audiobook server will be kept.")
    print()
    answer = input("Type DELETE to confirm this mode: ").strip()

    if answer != "DELETE":
        print("Delete mode not confirmed. Exiting without moving or deleting anything.")
        raise SystemExit(1)


def choose_template(cfg: Dict[str, Any], meta: TrackMeta) -> str:
    if (meta.series or "").strip():
        return cfg["templates"]["with_series"]
    return cfg["templates"]["without_series"]


def render_path(
    tpl: str,
    meta: TrackMeta,
    src: Path,
    space_repl: str,
    replace_author_spaces: bool,
) -> Path:
    # Author
    author_raw = meta.author
    if replace_author_spaces:
        author_raw = apply_space_repl(author_raw, space_repl)

    # Always apply rule to these
    title_raw = apply_space_repl(meta.title_best, space_repl)
    series_raw = apply_space_repl(meta.series, space_repl)
    album_raw = apply_space_repl(meta.album, space_repl)

    mapping = {
        "album":       sanitize(album_raw) or "_",
        "artist":      sanitize(meta.artist) or "_",
        "albumartist": sanitize(meta.albumartist) or "_",
        "author":      sanitize(author_raw) or "_",
        "title":       sanitize(title_raw) or src.stem,
        "narrator":    sanitize(meta.narrator) or "Unknown",
        "year":        meta.year_best or "0000",
        "series":      sanitize(series_raw) or "_",
        "series_part": sanitize(meta.series_part) or "00",
        "ext":         src.suffix.lstrip(".").lower() or "m4b",
    }

    rendered = tpl.format_map(mapping)
    rendered = rendered.replace("\\", os.sep).replace("/", os.sep)
    return Path(rendered)


def iter_audio_files(root: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        yield from (
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        )
    else:
        yield from (
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        )


def overwrite_existing_file(src: Path, dst: Path) -> None:
    """
    Replace an existing destination file with src.

    shutil.move() behavior with an existing destination file can vary by platform
    and filesystem situation. Removing the destination first makes the intent
    explicit.
    """
    dst.unlink()
    shutil.move(str(src), str(dst))


def resolve_duplicate(
    *,
    policy: DuplicatePolicy,
    src: Path,
    dst: Path,
    meta: TrackMeta,
    dryrun: bool,
) -> tuple[str, bool]:
    """
    Handle a duplicate destination.

    Returns:
        (action_description, file_was_moved)

    action_description is one of:
        overwritten, skipped, deleted, would overwrite, would skip, would delete
    """
    action = policy

    if policy == "ask":
        action = ask_duplicate_action(existing=dst, new=src, new_meta=meta)

    if dryrun:
        if action == "force":
            return "would overwrite", False
        if action == "delete":
            return "would delete", False
        return "would skip", False

    if action == "force":
        overwrite_existing_file(src, dst)
        print(f"\nOverwrote existing file: {dst}")
        return "overwritten", True

    if action == "delete":
        src.unlink()
        print(f"\nDeleted duplicate source file: {src}")
        return "deleted", False

    print(f"\nSkipping {src.name} (already exists at {dst})")
    return "skipped", False


# ----------------- main ----------------- #

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Move/simulate moving audiobook files based on metadata templates."
    )
    ap.add_argument("root", nargs="?", default=".", help="Directory to scan (default: .)")
    ap.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config YAML (default: ~/.config/abs/config.yml)",
    )
    ap.add_argument(
        "--dryrun", "-n",
        action="store_true",
        help="Do not move/delete files; write CSV of intended destinations",
    )
    ap.add_argument(
        "--csv",
        default="abs_dryrun.csv",
        help="CSV path for --dryrun output (default: abs_dryrun.csv)",
    )
    ap.add_argument(
        "--duplicates",
        choices=("ask", "force", "skip", "delete"),
        default="ask",
        help=(
            "What to do if the destination file already exists: "
            "'ask' prompts for each duplicate (default), "
            "'force' overwrites, "
            "'skip' leaves both files in place, "
            "'delete' deletes the source/new file."
        ),
    )
    ap.add_argument(
        "--skip-existing",
        dest="legacy_skip_existing",
        action="store_true",
        default=None,
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--no-skip-existing",
        dest="legacy_skip_existing",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--replace_spaces",
        default="_",
        help="String to replace spaces in series/title (and optionally author); "
             "use 'no_spaces' to remove spaces entirely",
    )
    ap.add_argument(
        "--replace_author_spaces",
        action="store_true",
        help="Also apply --replace_spaces rule to author folder",
    )

    args = ap.parse_args()

    duplicates: DuplicatePolicy = args.duplicates

    # Backward compatibility for the old duplicate flags.
    # --skip-existing behaves like --duplicates skip.
    # --no-skip-existing behaves like --duplicates force.
    if args.legacy_skip_existing is True:
        duplicates = "skip"
    elif args.legacy_skip_existing is False:
        duplicates = "force"

    if duplicates == "delete" and not args.dryrun:
        confirm_delete_mode()

    # Normalize/validate replacement string
    space_repl = args.replace_spaces
    if space_repl == "no_spaces":
        space_repl = ""

    invalid_chars = '<>:"/\\|?*' + "".join(chr(i) for i in range(0, 32))
    if any(ch in invalid_chars for ch in space_repl):
        illegal_list = " ".join(sorted(set(invalid_chars.replace("\n", "\\n"))))
        ap.error(
            "Invalid --replace_spaces value: contains forbidden characters. "
            f"Forbidden set includes: {illegal_list}"
        )

    cfg = load_config(Path(args.config))
    recursive = bool(cfg.get("options", {}).get("recursive", False))

    root = Path(args.root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        ap.error(f"Root directory not found or not a directory: {root}")

    files = list(iter_audio_files(root, recursive))
    total = len(files)

    rows: list[list[str]] = []
    moved = 0
    scanned = 0
    overwritten = 0
    skipped = 0
    deleted = 0
    duplicate_count = 0

    for idx, src in enumerate(files, start=1):
        print(
            f"Processing file {idx}/{total} ({(idx / total * 100) if total else 0:.1f}% complete)",
            end="\r",
            flush=True,
        )
        scanned += 1

        meta = extract_meta(src)
        tpl = choose_template(cfg, meta)
        dst_rel = render_path(tpl, meta, src, space_repl, args.replace_author_spaces)

        if dst_rel.is_absolute():
            dst = dst_rel
        else:
            # Relative templates are interpreted relative to source's parent
            dst = (src.parent / dst_rel).resolve()

        if args.dryrun:
            rows.append([
                meta.album or "",
                meta.albumartist or meta.artist or "",
                meta.narrator or "",
                meta.year_best or "",
                meta.series or "",
                meta.series_part or "",
                str(dst),
            ])
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        if dst.exists():
            duplicate_count += 1
            action_description, was_moved = resolve_duplicate(
                policy=duplicates,
                src=src,
                dst=dst,
                meta=meta,
                dryrun=args.dryrun,
            )

            if was_moved:
                moved += 1

            if action_description == "overwritten":
                overwritten += 1
            elif action_description == "skipped":
                skipped += 1
            elif action_description == "deleted":
                deleted += 1

            continue

        shutil.move(str(src), str(dst))
        moved += 1

    print()  # newline after progress output

    if args.dryrun:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["Album", "Artist", "Narrator", "Year", "Series", "Series-Part", "Filename"]
            )
            writer.writerows(rows)
        print(f"Scanned {scanned} file(s). Wrote dry run plan to {args.csv} with {len(rows)} row(s).")
    else:
        print(f"Scanned {scanned} file(s). Moved {moved} file(s).")
        if duplicate_count:
            print(
                "Duplicates handled: "
                f"{duplicate_count} found, "
                f"{overwritten} overwritten, "
                f"{skipped} skipped, "
                f"{deleted} deleted."
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
