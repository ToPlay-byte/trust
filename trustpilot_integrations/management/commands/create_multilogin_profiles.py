import csv
import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from trustpilot_integrations.models import TrustpilotAccount, TrustpilotMultiloginProfile
from trustpilot_integrations.multilogin import (
    MultiloginApiError,
    MultiloginClient,
    build_profile_payload,
)


TRUTHY_VALUES = {"1", "true", "yes", "y", "authorized", "ok"}


class Command(BaseCommand):
    help = "Create Multilogin profiles and bind them to authorized Trustpilot accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            type=Path,
            help="Optional CSV file with account emails/external IDs. If omitted, use --account-id or --email.",
        )
        parser.add_argument("--account-id", type=int, help="TrustpilotAccount database ID.")
        parser.add_argument("--email", help="TrustpilotAccount email.")
        parser.add_argument("--folder-id", required=True, help="Multilogin folder ID.")
        parser.add_argument("--token-env", default="MULTILOGIN_TOKEN", help="Env variable with Multilogin token.")
        parser.add_argument("--browser-type", default="mimic", choices=("mimic", "stealthfox"))
        parser.add_argument("--os-type", default="windows", choices=("windows", "macos", "linux"))
        parser.add_argument("--start-url", default="https://www.trustpilot.com/")
        parser.add_argument("--core-version", type=int, default=None)
        parser.add_argument("--auto-update-core", action="store_true", default=None)
        parser.add_argument("--no-auto-update-core", action="store_false", dest="auto_update_core")
        parser.add_argument("--timeout", type=int, default=60)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--allow-existing",
            action="store_true",
            help="Do not fail if account already has a Multilogin profile; skip it instead.",
        )

    def handle(self, *args, **options):
        token = os.environ.get(options["token_env"], "")
        dry_run = options["dry_run"]

        if not dry_run and not token:
            raise CommandError(f"Multilogin token is missing. Set {options['token_env']}.")

        accounts = self._get_accounts(options)
        if not accounts:
            self.stdout.write(self.style.WARNING("No accounts found."))
            return

        client = MultiloginClient(token=token, timeout=options["timeout"])

        created_count = 0
        skipped_count = 0
        failed_count = 0

        for account, row_data in accounts:
            if not account.is_authorized:
                skipped_count += 1
                self.stdout.write(
                    self.style.WARNING(f"Skipped {account.email}: account is not authorized.")
                )
                continue

            if hasattr(account, "multilogin_profile"):
                message = f"Account {account.email} already has Multilogin profile."
                if options["allow_existing"]:
                    skipped_count += 1
                    self.stdout.write(self.style.WARNING(f"Skipped: {message}"))
                    continue
                raise CommandError(message)

            profile_name = row_data.get("profile_name") or self._default_profile_name(account)
            notes = row_data.get("notes") or account.notes or ""
            start_url = row_data.get("start_url") or options["start_url"]
            proxy = self._build_proxy(row_data)

            payload = build_profile_payload(
                name=profile_name,
                folder_id=options["folder_id"],
                notes=notes,
                browser_type=options["browser_type"],
                os_type=options["os_type"],
                start_url=start_url,
                core_version=options["core_version"],
                auto_update_core=options["auto_update_core"],
                proxy=proxy,
            )

            if dry_run:
                skipped_count += 1
                self.stdout.write(
                    self.style.NOTICE(
                        f"DRY RUN: would create profile '{profile_name}' for {account.email}"
                    )
                )
                continue

            try:
                result = client.create_profile(payload)
            except MultiloginApiError as exc:
                failed_count += 1
                self.stderr.write(self.style.ERROR(f"Failed {account.email}: {exc}"))
                continue

            profile_id = result.profile_ids[0]

            try:
                with transaction.atomic():
                    TrustpilotMultiloginProfile.objects.create(
                        trustpilot_account=account,
                        multilogin_profile_id=profile_id,
                        multilogin_profile_name=profile_name,
                        multilogin_folder_id=options["folder_id"],
                        browser_type=options["browser_type"],
                        os_type=options["os_type"],
                        status=TrustpilotMultiloginProfile.Status.BOUND,
                        api_response=result.response,
                        notes=notes,
                    )
            except Exception as exc:
                failed_count += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"Profile {profile_id} was created in Multilogin, but DB binding failed for "
                        f"{account.email}: {exc}"
                    )
                )
                continue

            created_count += 1
            self.stdout.write(
                self.style.SUCCESS(f"Created profile {profile_id} and bound it to {account.email}.")
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {created_count}. Skipped: {skipped_count}. Failed: {failed_count}."
            )
        )

    def _get_accounts(self, options) -> list[tuple[TrustpilotAccount, dict[str, str]]]:
        if options.get("csv"):
            return self._get_accounts_from_csv(options["csv"])

        if options.get("account_id"):
            account = TrustpilotAccount.objects.get(id=options["account_id"])
            return [(account, {})]

        if options.get("email"):
            account = TrustpilotAccount.objects.get(email=options["email"])
            return [(account, {})]

        raise CommandError("Pass --csv, --account-id, or --email.")

    def _get_accounts_from_csv(self, csv_path: Path) -> list[tuple[TrustpilotAccount, dict[str, str]]]:
        if not csv_path.exists():
            raise CommandError(f"CSV file was not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames:
                raise CommandError("CSV file is empty or has no headers.")

            accounts = []
            for line_number, row in enumerate(reader, start=2):
                row_data = {key: str(value or "").strip() for key, value in row.items() if key}

                if not any(row_data.values()):
                    continue

                if "authorized" in row_data and row_data["authorized"].lower() not in TRUTHY_VALUES:
                    self.stdout.write(
                        self.style.WARNING(f"Line {line_number}: skipped, not authorized.")
                    )
                    continue

                account = self._find_account(row_data, line_number)
                accounts.append((account, row_data))

        return accounts

    def _find_account(self, row_data: dict[str, str], line_number: int) -> TrustpilotAccount:
        account_id = row_data.get("trustpilot_account_id") or row_data.get("account_id")
        email = row_data.get("trustpilot_email") or row_data.get("email")
        external_id = row_data.get("external_id")

        try:
            if account_id:
                return TrustpilotAccount.objects.get(id=account_id)
            if external_id:
                return TrustpilotAccount.objects.get(external_id=external_id)
            if email:
                return TrustpilotAccount.objects.get(email=email)
        except TrustpilotAccount.DoesNotExist as exc:
            raise CommandError(f"Line {line_number}: TrustpilotAccount was not found.") from exc

        raise CommandError(
            f"Line {line_number}: provide trustpilot_account_id, account_id, external_id, trustpilot_email, or email."
        )

    def _build_proxy(self, row_data: dict[str, str]) -> dict[str, Any] | None:
        proxy_host = row_data.get("proxy_host")
        if not proxy_host:
            return None

        proxy_port = row_data.get("proxy_port")
        if not proxy_port:
            raise CommandError("proxy_port is required when proxy_host is provided.")

        try:
            port = int(proxy_port)
        except ValueError as exc:
            raise CommandError("proxy_port must be an integer.") from exc

        proxy = {
            "type": row_data.get("proxy_type") or "http",
            "host": proxy_host,
            "port": port,
            "save_traffic": False,
        }

        if row_data.get("proxy_username"):
            proxy["username"] = row_data["proxy_username"]
        if row_data.get("proxy_password"):
            proxy["password"] = row_data["proxy_password"]

        return proxy

    def _default_profile_name(self, account: TrustpilotAccount) -> str:
        return f"Trustpilot {account.id} {account.email}"
