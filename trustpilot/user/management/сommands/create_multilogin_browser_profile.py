import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from user.models import Profile, User


BASE_DIR = Path(__file__).resolve().parents[3]
MODULES_DIR = BASE_DIR / "modules"

if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from clients.multilogin_client import MultiloginApiError, MultiloginClient


class Command(BaseCommand):
    help = "Create Multilogin browser profile and save ml_profile_id / ml_folder_id into existing Django Profile model."

    def add_arguments(self, parser):
        parser.add_argument(
            "--profile-id",
            type=int,
            help="Existing Profile ID. If passed, command updates this Profile with created Multilogin profile ID.",
        )
        parser.add_argument(
            "--profile-email",
            help="Existing Profile email. Alternative to --profile-id.",
        )

        parser.add_argument(
            "--user-id",
            type=int,
            help="Existing User ID with Multilogin credentials. Used when creating a new local Profile row.",
        )
        parser.add_argument(
            "--user-email",
            help="Existing User email with Multilogin credentials. Alternative to --user-id.",
        )

        parser.add_argument(
            "--profile-name",
            help="Profile name for Multilogin and local Profile row.",
        )
        parser.add_argument(
            "--account-email",
            help="Optional email to save into Profile.email.",
        )

        parser.add_argument(
            "--folder-id",
            required=True,
            help="Multilogin folder ID where browser profile will be created.",
        )

        parser.add_argument(
            "--browser-type",
            default="mimic",
            choices=("mimic", "stealthfox"),
        )
        parser.add_argument(
            "--os-type",
            default="windows",
            choices=("windows", "macos", "linux"),
        )
        parser.add_argument(
            "--start-url",
            default="https://www.trustpilot.com/",
        )
        parser.add_argument(
            "--core-version",
            type=int,
            default=None,
        )
        parser.add_argument(
            "--auto-update-core",
            action="store_true",
            default=None,
        )
        parser.add_argument(
            "--no-auto-update-core",
            action="store_false",
            dest="auto_update_core",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=60,
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow replacing existing ml_profile_id on Profile.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not call Multilogin API and do not save changes.",
        )

    def handle(self, *args, **options):
        profile = self._get_existing_profile(options)

        if profile:
            user = profile.user
            profile_name = options.get("profile_name") or profile.profile_name
            account_email = profile.email
        else:
            user = self._get_user(options)
            profile_name = options.get("profile_name")
            account_email = options.get("account_email")

            if not profile_name:
                raise CommandError("Pass --profile-name when creating a new local Profile row.")

        folder_id = options["folder_id"]

        if not user.password:
            raise CommandError(f"User {user.email} does not have Multilogin password saved.")

        if profile and profile.ml_profile_id and not options["force"]:
            raise CommandError(
                f"Profile {profile.id} already has ml_profile_id={profile.ml_profile_id}. "
                f"Use --force if you want to replace it."
            )

        payload = MultiloginClient.build_profile_payload(
            name=profile_name,
            folder_id=folder_id,
            browser_type=options["browser_type"],
            os_type=options["os_type"],
            start_url=options["start_url"],
            core_version=options["core_version"],
            auto_update_core=options["auto_update_core"],
            notes=f"Django Profile: {profile_name}",
        )

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: would create Multilogin profile '{profile_name}' "
                    f"under User {user.email} in folder {folder_id}."
                )
            )
            return

        try:
            token = MultiloginClient.get_token_by_credentials(
                email=user.email,
                password=user.password,
                timeout=options["timeout"],
            )
        except MultiloginApiError as exc:
            raise CommandError(f"Failed to get Multilogin token for User {user.email}: {exc}") from exc

        try:
            result = MultiloginClient.create_profile_with_token(
                token=token,
                payload=payload,
                timeout=options["timeout"],
            )
        except MultiloginApiError as exc:
            raise CommandError(f"Failed to create Multilogin profile: {exc}") from exc

        ml_profile_id = result.profile_ids[0]

        with transaction.atomic():
            if profile:
                profile.profile_name = profile_name
                profile.ml_profile_id = ml_profile_id
                profile.ml_folder_id = folder_id

                if account_email and not profile.email:
                    profile.email = account_email

                profile.save(
                    update_fields=[
                        "profile_name",
                        "ml_profile_id",
                        "ml_folder_id",
                        "email",
                    ]
                )
            else:
                profile = Profile.objects.create(
                    user=user,
                    profile_name=profile_name,
                    email=account_email or None,
                    ml_profile_id=ml_profile_id,
                    ml_folder_id=folder_id,
                    stage_status=Profile.StageStatus.NOT_STARTED,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Multilogin profile {ml_profile_id} and saved it to Django Profile ID {profile.id}."
            )
        )

    def _get_existing_profile(self, options):
        profile_id = options.get("profile_id")
        profile_email = options.get("profile_email")

        if profile_id and profile_email:
            raise CommandError("Use only one option: --profile-id or --profile-email.")

        if profile_id:
            try:
                return Profile.objects.select_related("user").get(id=profile_id)
            except Profile.DoesNotExist as exc:
                raise CommandError(f"Profile with id={profile_id} was not found.") from exc

        if profile_email:
            try:
                return Profile.objects.select_related("user").get(email=profile_email)
            except Profile.DoesNotExist as exc:
                raise CommandError(f"Profile with email={profile_email} was not found.") from exc

        return None

    def _get_user(self, options):
        user_id = options.get("user_id")
        user_email = options.get("user_email")

        if user_id and user_email:
            raise CommandError("Use only one option: --user-id or --user-email.")

        if user_id:
            try:
                return User.objects.get(id=user_id)
            except User.DoesNotExist as exc:
                raise CommandError(f"User with id={user_id} was not found.") from exc

        if user_email:
            try:
                return User.objects.get(email=user_email)
            except User.DoesNotExist as exc:
                raise CommandError(f"User with email={user_email} was not found.") from exc

        raise CommandError(
            "Pass --profile-id / --profile-email to update existing Profile, "
            "or --user-id / --user-email to create a new Profile row."
        )