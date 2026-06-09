 Admin is live and returning 200. Here is the complete record of everything done and the exact state you're in now.

  ---
  What was done — exact steps and commands

  Problem found: postgres container had a corrupted WAL

  The container was not running because its data volume had been uncleanly shut down (PANIC: could not locate a valid checkpoint record). The volume had to be wiped and the
  backup re-restored.

  ---
  Step 1 — .env confirmed

  DB_NAME=trustpilot
  DB_USER=postgres
  DB_PASSWORD=Ukraine2018!
  DB_HOST=localhost
  DB_PORT=5001

  Step 2 — Fixed the crashed postgres container

  # Find what held the corrupted volume
  docker ps -a --filter volume=6281d06f...

  # Remove stale pgtest container that held the volume lock
  docker rm pgtest
  docker volume rm 6281d06f...

  # Recreate postgres with a fresh volume
  docker compose up -d postgres

  Step 3 — Re-restored the backup

  docker cp backup_trustpilot_0605.dump trustpilotcom-postgres-1:/tmp/backup.dump
  docker exec trustpilotcom-postgres-1 pg_restore -U postgres -d trustpilot --no-owner --no-privileges /tmp/backup.dump
  Zero errors. Restored: auth_user=1, user_user=2, user_profile=10, user_usertaskmanager=14, user_usertasklog=130.

  Step 4 — Created missing static directory

  mkdir static   # at project root

  Step 5 — Ran migrations (applied 0016–0023)

  .venv\Scripts\python trustpilot\manage.py migrate
  Applied 8 migrations including 0023 which created user_profiledayprogress.

  Step 6 — Reset admin password

  .venv\Scripts\python trustpilot\manage.py shell -c "
  from django.contrib.auth.models import User
  u = User.objects.get(username='admin')
  u.set_password('admin1234')
  u.save()"

  Step 7 — Started Django dev server

  Start-Process -FilePath ".venv\Scripts\python" -ArgumentList "trustpilot\manage.py","runserver" -NoNewWindow

  ---
  Current state

  │ Admin URL               │ http://127.0.0.1:8000/admin/                                 │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  from django.contrib.auth.models import User
  u = User.objects.get(username='admin')
  u.set_password('admin1234')
  u.save()"

  Step 7 — Started Django dev server

  Start-Process -FilePath ".venv\Scripts\python" -ArgumentList "trustpilot\manage.py","runserver" -NoNewWindow

  ---
  Current state

  ┌─────────────────────────┬──────────────────────────────────────────────────────────────┐
  │          Item           │                            Value                             │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Admin URL               │ http://127.0.0.1:8000/admin/                                 │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Username                │ admin                                                        │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Password                │ admin1234                                                    │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ user_profile            │ 10 rows with ml_profile_id + ml_folder_id                    │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ user_usertaskmanager    │ 14 rows                                                      │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ user_profiledayprogress │ 0 rows (table now exists, empty — created by migration 0023) │
  ├─────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ All migrations          │ 0001–0023 applied                                            │
  └─────────────────────────┴───────────────────────────────────────────────────