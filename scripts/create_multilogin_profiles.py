#!/usr/bin/env python3
"""
Create Multilogin profiles for authorized Trustpilot account records and store a local mapping.

This script only provisions Multilogin profiles and records which authorized Trustpilot
account each profile belongs to. It does not create Trustpilot accounts, log in to
Trustpilot, publish reviews, manipulate ratings, or bypass platform rules.

Usage example:
    export MULTILOGIN_TOKEN="your-token"

    python scripts/create_multilogin_profiles.py \
        --input examples/trustpilot_accounts.example.csv \
        --folder-id 4500dd84-d8c5-4450-b2df-1c64daed8bad \
        --sqlite-db data/profile_mappings.sqlite3 \
        --output data/profile_creation_result.csv

Dry run:
    python scripts/create_multilogin_profiles.py \
        --input examples/trustpilot_accounts.example.csv \
        --folder-id 4500dd84-d8c5-4450-b2df-1c64daed8bad \
        --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MULTILOGIN_PROFILE_CREATE_URL = "https://api.multilogin.com/profile/create"
REQUIRED_COLUMNS = ("trustpilot_account_id", "trustpilot_email", "profile_name")
TRUTHY_VALUES = {"1", "true", "yes", "y", "authorized", "ok"}


@dataclass(frozen=True)
class AccountRecord:
    trustpilot_account_id: str
    trustpilot_email: str
    profile_name: str
    notes: str
    authorized: bool
    raw: dict[str, str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_bool(value: str, default: bool = False) -> bool:
    value = clean(value).lower()
    if not value:
        return default
    return value in TRUTHY_VALUES


def read_accounts(csv_path: Path) -> list[AccountRecord]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV file was not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError("Input CSV file is empty or has no header row.")

        missing_columns = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing_columns:
            raise ValueError(
                "Input CSV is missing required columns: " + ", ".join(missing_columns)
            )

        records: list[AccountRecord] = []
        for line_number, row in enumerate(reader, start=2):
            normalized = {key: clean(value) for key, value in row.items() if key is not None}

            if not any(normalized.values()):
                continue

            account_id = normalized.get("trustpilot_account_id", "")
            email = normalized.get("trustpilot_email", "")
            profile_name = normalized.get("profile_name", "")

            if not account_id or not email or not profile_name:
                raise ValueError(
                    f"Line {line_number}: trustpilot_account_id, trustpilot_email and "
                    "profile_name are required."
                )

            # If the CSV contains an `authorized` column, it must be truthy.
            # If the column is absent, the script assumes the input file already contains
            # only accounts that are owned/managed by the operator.
            if "authorized" in normalized:
                authorized = parse_bool(normalized.get("authorized", ""), default=False)
            else:
                authorized = True

            records.append(
                AccountRecord(
                    trustpilot_account_id=account_id,
                    trustpilot_email=email,
                    profile_name=profile_name,
                    notes=normalized.get("notes", ""),
                    authorized=authorized,
                    raw=normalized,
                )
            )

    return records


def ensure_parent_dir(path: Path) -> None:
    if path.parent and str(path.parent) != ".":
        path.parent.mkdir(parents=True, exist_ok=True)


def init_db(db_path: Path) -> sqlite3.Connection:
    ensure_parent_dir(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trustpilot_multilogin_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trustpilot_account_id TEXT NOT NULL UNIQUE,
            trustpilot_email TEXT NOT NULL,
            multilogin_profile_id TEXT NOT NULL UNIQUE,
            multilogin_profile_name TEXT NOT NULL,
            binding_status TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def account_is_already_bound(connection: sqlite3.Connection, trustpilot_account_id: str) -> bool:
    cursor = connection.execute(
        """
        SELECT 1
        FROM trustpilot_multilogin_profiles
        WHERE trustpilot_account_id = ?
        LIMIT 1
        """,
        (trustpilot_account_id,),
    )
    return cursor.fetchone() is not None


def insert_mapping(
    connection: sqlite3.Connection,
    record: AccountRecord,
    multilogin_profile_id: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO trustpilot_multilogin_profiles (
            trustpilot_account_id,
            trustpilot_email,
            multilogin_profile_id,
            multilogin_profile_name,
            binding_status,
            notes,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.trustpilot_account_id,
            record.trustpilot_email,
            multilogin_profile_id,
            record.profile_name,
            "bound",
            record.notes,
            now,
            now,
        ),
    )
    connection.commit()


def build_proxy(raw: dict[str, str]) -> dict[str, Any] | None:
    proxy_host = raw.get("proxy_host", "")
    if not proxy_host:
        return None

    proxy_type = raw.get("proxy_type", "http") or "http"
    proxy_port_raw = raw.get("proxy_port", "")

    if not proxy_port_raw:
        raise ValueError(f"proxy_port is required when proxy_host is set for {proxy_host}.")

    try:
        proxy_port = int(proxy_port_raw)
    except ValueError as exc:
        raise ValueError(f"proxy_port must be an integer for proxy_host {proxy_host}.") from exc

    proxy: dict[str, Any] = {
        "type": proxy_type,
        "host": proxy_host,
        "port": proxy_port,
        "save_traffic": False,
    }

    proxy_username = raw.get("proxy_username", "")
    proxy_password = raw.get("proxy_password", "")
    if proxy_username:
        proxy["username"] = proxy_username
    if proxy_password:
        proxy["password"] = proxy_password

    return proxy


def build_payload(record: AccountRecord, args: argparse.Namespace) -> dict[str, Any]:
    start_url = record.raw.get("start_url") or args.start_url

    payload: dict[str, Any] = {
        "name": record.profile_name,
        "browser_type": args.browser_type,
        "folder_id": args.folder_id,
        "os_type": args.os_type,
        "times": 1,
        "notes": record.notes,
        "parameters": {
            "storage": {
                "is_local": args.local_storage,
                "save_service_worker": args.save_service_worker,
            },
            "custom_start_urls": [start_url],
        },
    }

    # Recommended default: let Multilogin use the current browser core unless a specific
    # version is explicitly supplied.
    if args.core_version is not None:
        payload["core_version"] = args.core_version
    if args.auto_update_core is not None:
        payload["auto_update_core"] = args.auto_update_core

    proxy = build_proxy(record.raw)
    if proxy:
        payload["parameters"]["proxy"] = proxy

    return payload


def sanitized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(payload))
    proxy = cloned.get("parameters", {}).get("proxy")
    if isinstance(proxy, dict):
        if "username" in proxy:
            proxy["username"] = "***"
        if "password" in proxy:
            proxy["password"] = "***"
    return cloned


def post_json(url: str, token: str, payload: dict[str, Any], timeout: int) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body or "{}")
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        try:
            parsed_body = json.loads(response_body or "{}")
        except json.JSONDecodeError:
            parsed_body = {"raw_body": response_body}
        return error.code, parsed_body
    except urllib.error.URLError as error:
        return 0, {"error": str(error)}


def extract_created_profile_ids(http_code: int, response_json: dict[str, Any]) -> tuple[list[str], str]:
    status = response_json.get("status")
    message = ""
    if isinstance(status, dict):
        message = clean(status.get("message"))

    if http_code != 201:
        return [], message or "Profile creation failed"

    data = response_json.get("data")
    if not isinstance(data, dict):
        return [], message or "Response does not contain data object"

    ids = data.get("ids")
    if not isinstance(ids, list):
        return [], message or "Response does not contain data.ids list"

    return [clean(profile_id) for profile_id in ids if clean(profile_id)], message


def write_results(output_path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_parent_dir(output_path)
    fieldnames = [
        "timestamp",
        "trustpilot_account_id",
        "trustpilot_email",
        "profile_name",
        "multilogin_profile_id",
        "status",
        "http_code",
        "message",
    ]

    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def result_row(
    record: AccountRecord,
    status: str,
    message: str,
    http_code: int | str = "",
    multilogin_profile_id: str = "",
) -> dict[str, Any]:
    return {
        "timestamp": utc_now(),
        "trustpilot_account_id": record.trustpilot_account_id,
        "trustpilot_email": record.trustpilot_email,
        "profile_name": record.profile_name,
        "multilogin_profile_id": multilogin_profile_id,
        "status": status,
        "http_code": http_code,
        "message": message,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Multilogin profiles and bind them to authorized Trustpilot accounts locally."
    )
    parser.add_argument("--input", required=True, type=Path, help="CSV file with Trustpilot account records.")
    parser.add_argument("--folder-id", required=True, help="Multilogin folder ID where profiles will be created.")
    parser.add_argument("--sqlite-db", type=Path, default=Path("data/profile_mappings.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("data/profile_creation_result.csv"))
    parser.add_argument("--token-env", default="MULTILOGIN_TOKEN", help="Environment variable with Multilogin token.")
    parser.add_argument("--browser-type", default="mimic", choices=("mimic", "stealthfox"))
    parser.add_argument("--os-type", default="windows", choices=("windows", "macos", "linux"))
    parser.add_argument("--start-url", default="https://www.trustpilot.com/")
    parser.add_argument("--core-version", type=int, default=None)
    parser.add_argument("--auto-update-core", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--local-storage", action="store_true", help="Set parameters.storage.is_local to true.")
    parser.add_argument("--save-service-worker", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep", type=float, default=0.3, help="Delay between API calls in seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Build payloads without calling Multilogin API.")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit with code 1 if any row fails.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(args.token_env, "")

    if not args.dry_run and not token:
        print(
            f"ERROR: Multilogin token is missing. Set environment variable {args.token_env}.",
            file=sys.stderr,
        )
        return 2

    try:
        records = read_accounts(args.input)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if not records:
        print("No account records found in input CSV.")
        return 0

    connection = init_db(args.sqlite_db)
    results: list[dict[str, Any]] = []
    seen_account_ids: set[str] = set()
    has_failure = False

    for index, record in enumerate(records, start=1):
        prefix = f"[{index}/{len(records)}] {record.trustpilot_email}"

        if not record.authorized:
            message = "Skipped: account is not marked as authorized in input CSV."
            print(f"{prefix}: {message}")
            results.append(result_row(record, "skipped", message))
            continue

        if record.trustpilot_account_id in seen_account_ids:
            message = "Skipped: duplicate Trustpilot account ID in input CSV."
            print(f"{prefix}: {message}")
            results.append(result_row(record, "skipped", message))
            continue
        seen_account_ids.add(record.trustpilot_account_id)

        if account_is_already_bound(connection, record.trustpilot_account_id):
            message = "Skipped: Trustpilot account is already bound in local SQLite database."
            print(f"{prefix}: {message}")
            results.append(result_row(record, "skipped", message))
            continue

        try:
            payload = build_payload(record, args)
        except ValueError as error:
            has_failure = True
            message = str(error)
            print(f"{prefix}: ERROR: {message}")
            results.append(result_row(record, "error", message))
            continue

        if args.dry_run:
            print(f"{prefix}: DRY RUN payload:")
            print(json.dumps(sanitized_payload(payload), indent=2, ensure_ascii=False))
            results.append(result_row(record, "dry_run", "Payload built successfully."))
            continue

        http_code, response_json = post_json(
            MULTILOGIN_PROFILE_CREATE_URL,
            token=token,
            payload=payload,
            timeout=args.timeout,
        )
        created_ids, message = extract_created_profile_ids(http_code, response_json)

        if not created_ids:
            has_failure = True
            safe_message = message or "Unknown Multilogin API error."
            print(f"{prefix}: ERROR: {safe_message} HTTP={http_code}")
            results.append(result_row(record, "error", safe_message, http_code=http_code))
            time.sleep(args.sleep)
            continue

        # The script sends times=1, so the normal case is exactly one ID.
        # If the API returns more IDs, bind only the first to avoid creating multiple
        # profiles for the same Trustpilot account.
        profile_id = created_ids[0]
        try:
            insert_mapping(connection, record, profile_id)
        except sqlite3.IntegrityError as error:
            has_failure = True
            safe_message = f"Profile was created, but local binding failed: {error}"
            print(f"{prefix}: ERROR: {safe_message}")
            results.append(
                result_row(
                    record,
                    "error",
                    safe_message,
                    http_code=http_code,
                    multilogin_profile_id=profile_id,
                )
            )
            time.sleep(args.sleep)
            continue

        print(f"{prefix}: created and bound Multilogin profile {profile_id}")
        results.append(
            result_row(
                record,
                "created",
                message or "Profile successfully created",
                http_code=http_code,
                multilogin_profile_id=profile_id,
            )
        )
        time.sleep(args.sleep)

    write_results(args.output, results)
    connection.close()

    print(f"\nDone. Result CSV: {args.output}")
    print(f"SQLite mapping DB: {args.sqlite_db}")

    if has_failure and args.fail_on_error:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
