import argparse
import io
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout
from pathlib import Path

from common.session_splitting_utilities import SessionSplittingUtilities

CHANNEL_MAP_FILENAME = "channel-map.json"

TODO_SUFFIX = ".todo"
INPROG_SUFFIX = ".inprog"
DONE_SUFFIX = ".done"
FAILED_SUFFIX = ".failed"

MAX_WORKERS: int | None = 10


def get_bands(root_directory: str) -> list[str]:
    return [e.name for e in os.scandir(root_directory) if e.is_dir()]


def get_todos_for_band(root_directory: str, band_name: str) -> list[str]:
    path = f"{root_directory}/{band_name}/_raw"
    if not os.path.isdir(path):
        return []
    return [e.name for e in os.scandir(path) if e.is_dir() and e.name.endswith(TODO_SUFFIX)]


def get_tracks_for_raw(root_directory: str, band_name: str, raw_name: str) -> list[str]:
    return [e.name for e in os.scandir(f"{root_directory}/{band_name}/_raw/{raw_name}") if e.is_dir()]


def load_channel_map(root_directory: str, band_name: str, raw_name: str) -> list[str | None] | None:
    path = f"{root_directory}/{band_name}/_raw/{raw_name}/{CHANNEL_MAP_FILENAME}"
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array, got {type(data).__name__}")
    return data


def claim_raw(root_directory: str, band_name: str, todo_name: str) -> tuple[str, str] | None:
    raw_dir = Path(root_directory) / band_name / "_raw"
    base = todo_name[:-len(TODO_SUFFIX)]
    inprog_name = base + INPROG_SUFFIX
    try:
        (raw_dir / todo_name).rename(raw_dir / inprog_name)
    except OSError:
        return None
    return inprog_name, base


def finalise_raw(root_directory: str, band_name: str, inprog_name: str, base: str, success: bool) -> None:
    raw_dir = Path(root_directory) / band_name / "_raw"
    final_suffix = DONE_SUFFIX if success else FAILED_SUFFIX
    final_name = base + final_suffix
    try:
        (raw_dir / inprog_name).rename(raw_dir / final_name)
    except OSError as e:
        print(f"WARNING: could not rename {inprog_name} -> {final_name}: {e}", file=sys.stderr)


def run_one(ssu: SessionSplittingUtilities) -> tuple[str, bool, str, str | None]:
    label = str(ssu.input_folder_path)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            ok = ssu.go()
        return label, ok, buf.getvalue(), None
    except Exception as e:
        return label, False, buf.getvalue(), f"{type(e).__name__}: {e}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process multi-track recordings under a root directory."
    )
    parser.add_argument(
        "root-directory",
        help="Path to the multi-track recordings root directory",
    )
    args = parser.parse_args()
    root_directory = args.root_directory

    # one entry per claimed session: tracks the SSUs to run and a failure counter
    raw_groups: list[dict] = []

    for band_name in get_bands(root_directory):
        for todo_name in get_todos_for_band(root_directory, band_name):
            claimed = claim_raw(root_directory, band_name, todo_name)
            if claimed is None:
                # another run beat us to it, or transient FS issue - skip
                continue
            inprog_name, base = claimed

            try:
                channel_map = load_channel_map(root_directory, band_name, inprog_name)
            except (ValueError, json.JSONDecodeError) as e:
                print(f"WARNING: malformed channel map in {band_name}/{inprog_name}: {e}", file=sys.stderr)
                finalise_raw(root_directory, band_name, inprog_name, base, success=False)
                continue

            group_ssus: list[SessionSplittingUtilities] = []
            for track_name in get_tracks_for_raw(root_directory, band_name, inprog_name):
                group_ssus.append(SessionSplittingUtilities(
                    input_directory_path=f"{root_directory}/{band_name}/_raw/{inprog_name}/{track_name}",
                    output_directory_path=f"{root_directory}/{band_name}/{base}/{track_name}",
                    channel_names=channel_map,
                ))

            raw_groups.append({
                "band": band_name,
                "inprog_name": inprog_name,
                "base": base,
                "ssus": group_ssus,
                "failures": 0,
            })

    if not raw_groups:
        print("Nothing to do.")
        return

    # flatten all ssus across all groups, keeping the group index so we can attribute failures back to the right session for finalisation
    pool_input: list[tuple[int, SessionSplittingUtilities]] = []
    for gi, g in enumerate(raw_groups):
        for ssu in g["ssus"]:
            pool_input.append((gi, ssu))

    total = len(pool_input)
    print(f"Queued {total} task(s) across {len(raw_groups)} session(s) (max {MAX_WORKERS or os.cpu_count()} parallel workers).\n")

    failures: list[tuple[str, str]] = []
    sep = "=" * 100

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_one, ssu): (gi, ssu) for gi, ssu in pool_input}

        for i, future in enumerate(as_completed(futures), start=1):
            gi, _ssu = futures[future]
            label, ok, output, error = future.result()

            print(sep)
            print(f"[{i}/{total}] {label}")
            print(sep)
            if output:
                sys.stdout.write(output)
                if not output.endswith("\n"):
                    sys.stdout.write("\n")
            if error:
                print(f"FAILED: {error}", file=sys.stderr)
                failures.append((label, error))
                raw_groups[gi]["failures"] += 1
            elif not ok:
                # go() returned False (e.g. no valid channels) - count as failure
                raw_groups[gi]["failures"] += 1
            print()

    # finalise each session: .done if all tracks succeeded, else .failed
    print(sep)
    print("Session outcomes:")
    for g in raw_groups:
        all_ok = g["failures"] == 0
        status = "OK" if all_ok else f"{g['failures']} failure(s)"
        print(f"  {g['band']}/{g['base']}: {status}")
        finalise_raw(root_directory, g["band"], g["inprog_name"], g["base"], success=all_ok)

    print(sep)
    if failures:
        print(f"\n{len(failures)} task(s) failed:")
        for label, err in failures:
            print(f"  {label}")
            print(f"    {err}")
    else:
        print(f"\nAll {total} task(s) completed successfully.")


if __name__ == '__main__':
    main()
