# Nextcloud Upgrade Script

Upgrade an existing Nextcloud installation from the command line. The script
finds the next eligible stable release, downloads and checks it, backs up the
database and configuration, and installs the update while managing maintenance
mode, services and scheduled jobs.

It keeps the previous application files and recovery backups so that a failed
upgrade can be investigated and recovered. Downloads and extraction happen on
disk before the installation is taken offline.

## Requirements

The script supports a conventional Linux installation with:

- Bash, GNU command-line utilities and PHP CLI with SimpleXML, available as `php`.
- Nextcloud commands and cron jobs running as `www-data`.
- A web server and PHP-FPM with systemd service units. Both services must already
  be running. Nginx is the default web server; service names are configurable.
- Monit, when using the default service manager. Its service names must match
  the systemd unit names, and both services must already be monitored.
- MariaDB/MySQL or PostgreSQL. Database settings are read from Nextcloud.
- A user-data directory outside the Nextcloud installation directory.
- Enough disk space and inodes for the downloaded release, extracted files,
  staged installation and recovery backups.

The required utilities include `sudo`, `curl`, `systemctl`, `crontab`, `pgrep`,
`flock`, `findmnt`, `gzip`, and checksum tools. Monit is only required when
`SERVICE_MANAGER=monit`; select `systemd` to manage services directly. The default
archive format uses `tar` and `bzip2`; ZIP requires `unzip` and `zipinfo`.
For MariaDB/MySQL, install `mariadb` and `mariadb-dump`, or their `mysql` and `mysqldump` equivalents.
For PostgreSQL, install `psql` and `pg_dump`. The script checks required commands
before upgrading.

Installation and backup paths must be existing, canonical absolute paths without
symlinks. The backup directory must be root owned and not writable by group or
others. Symlinks or mounts inside the application directory, external configuration
directories, and external or nested custom app directories are not supported.

Keep a separate backup of Nextcloud user data. **This script backs up the database
and configuration, but does not back up user data.** Those recovery artifacts are
useful for an upgrade failure; recovering from data loss also requires a suitable
data backup. Independently scheduled database/server and data backups may not
represent the same point in time.

## Installation

Clone the repository on the server that runs Nextcloud:

```bash
git clone https://github.com/msanguineti/nextcloud-scripts.git
cd nextcloud-scripts
```

Install the script and create its default backup directory:

```bash
sudo install -o root -g root -m 0755 nextcloud-upgrade.sh /usr/local/sbin/nextcloud-upgrade
sudo install -d -o root -g root -m 0700 /var/backups/nextcloud
```

Alternatively, run `sudo ./nextcloud-upgrade.sh` from the checkout. To update an
installed copy, update your checkout, review the changes, and repeat the script
installation command.

Before the first upgrade, confirm that your installation matches the requirements,
review the target Nextcloud release notes and app compatibility, and verify that
your separate data backups are available. Schedule an interruption to web access
and background jobs.

## Running an upgrade

To find and install the next eligible stable update:

```bash
sudo nextcloud-upgrade
```

The script checks the installation, downloads and validates the release, stages
the new files, and checks database access and backup capacity. It then displays
the current and target versions and asks for confirmation. Enter `y` or `Y` to
continue; any other answer cancels. Cancellation at this prompt leaves the
installation running, although downloads and preparation have already occurred.

After confirmation, the script enables maintenance mode, suspends this
installation's cron entries, stops the web server and PHP-FPM through the selected
service manager, waits for existing Nextcloud CLI jobs to finish, and creates
the recovery backups. It then
replaces the application, runs `occ upgrade`, verifies the installed version and
database migration state, and restores services and the original cron schedule.

You can also supply a specific stable release within the currently installed
major version. For example, when upgrading 34.0.0 to 34.0.1:

```bash
sudo nextcloud-upgrade 34.0.1
```

Use automatic discovery for major upgrades. Downgrades and upgrades that skip a
major version are rejected. Automatic discovery requires the stable update
channel; Nextcloud's release requirements and compatibility guidance still apply.

After the script finishes, open Nextcloud, check Administration Overview, test a
file upload/download and client sync, and confirm that background jobs resume.
The script's local checks do not replace verification through your web interface.

## Options and examples

Configure the script with environment variables. Use `sudo env` to pass them to
the root process:

```bash
sudo env NEXTCLOUD_PATH=/srv/nextcloud \
    BACKUP_PATH=/var/backups/nextcloud \
    nextcloud-upgrade
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXTCLOUD_PATH` | `/var/www/nextcloud` | Nextcloud installation directory. |
| `BACKUP_PATH` | `/var/backups/nextcloud` | Directory for upgrade recovery backups. |
| `SERVICE_MANAGER` | `monit` | Service control backend: `monit` or `systemd`. |
| `WEB_SERVICE` | `nginx` | Web-server systemd unit name; also the Monit name when selected. |
| `PHP_SERVICE` | `php8.4-fpm` | PHP-FPM systemd unit name; also the Monit name when selected. |
| `WAIT_BEFORE_BACKUP` | `60` | Maximum seconds to wait for existing Nextcloud cron/occ processes after services stop. |
| `WAIT_AFTER_SERVER_START` | `20` | Maximum seconds to poll each service state after its start/stop command returns. |
| `DOWNLOAD_FORMAT` | `tar.bz2` | Release archive format: `tar.bz2` or `zip`. |
| `CHECKSUM_TYPE` | `sha256` | Checksum: `sha256`, `sha512`, or legacy `md5`. Prefer SHA256 or SHA512. |

To use systemd directly on a server without Monit supervision:

```bash
sudo env SERVICE_MANAGER=systemd nextcloud-upgrade
```

Keep the default `monit` setting when Monit supervises these services. Stopping a
service directly with systemd leaves Monit free to start it again. In systemd
mode, ensure no other supervisor or activation mechanism will restart the selected
services during the upgrade. The script does not switch backends automatically
or change supervisor configuration.

Both backends verify actual service state through systemd. The selected manager
is also used during failure recovery and recorded with the recovery artifacts.
Monit applies its configured dependencies: for example, if PHP-FPM depends on
Nginx, stopping Nginx through Monit also stops PHP-FPM. Startup may likewise
start a prerequisite service automatically; maintenance remains enabled until
the upgrade has been verified.

For a different PHP-FPM service and a longer service-state polling timeout:

```bash
sudo env PHP_SERVICE=php8.3-fpm WAIT_AFTER_SERVER_START=60 nextcloud-upgrade
```

The selected PHP version must support both the installed and target Nextcloud
versions. `PHP_SERVICE` selects the service; command-line operations still use
`php` from the root user's command path.

To use ZIP and SHA512:

```bash
sudo env DOWNLOAD_FORMAT=zip CHECKSUM_TYPE=sha512 nextcloud-upgrade
```

Checksum verification checks the downloaded archive against a checksum fetched
from the same HTTPS release server; it is not an independent signature check.
Cron entries are restored exactly, including their schedules and disabled entries.

## Custom apps, themes and configuration

The script transfers the complete `config/` directory, `.htaccess`, `.user.ini`,
custom themes, and additional direct-child app directories configured in
`apps_paths`. Apps in the old `apps/` directory are copied when absent from the
new release, unless their metadata marks them as shipped apps. When an app is
included in the new release, the new release's copy takes precedence.

Check custom app and theme compatibility before upgrading. Other custom files
are retained in the old installation but are not automatically transferred.
Ownership and permissions are applied only to the staged application:
`www-data:www-data`, directories `750`, and files `640`.

## Backups and troubleshooting

Each confirmed attempt creates a unique directory under `BACKUP_PATH`, named
`upgrade-TIMESTAMP.RANDOM`. After a successful backup it contains
`database.sql.gz` and the complete `config/` directory. It also records original
cron entries, recovery paths, progress and, once migration starts, `upgrade.log`.
Files ending in `.part` are incomplete backups.

The previous application is retained alongside the installation in a unique
`.old.upgrade-…` directory. Successful upgrades remove disposable download and
staging files, but keep the recovery backups and old application. Remove obsolete
recovery copies deliberately after verifying the upgrade; check their exact paths
and ensure they contain no user-data mounts before deleting them.

If an upgrade fails:

- **Before database migration:** the script attempts to restore the original
  application, services, cron and maintenance state. Confirm that recovery
  succeeded before retrying.
- **Once database migration has started:** the script attempts to block access
  and keeps recovery artifacts for manual intervention. Do not restore old
  application files alone against a potentially migrated database. Resolve and
  resume the target upgrade, or restore matching database, configuration and
  application backups after assessing user-data consistency.

Errors identify the failed operation or script line and the recovery location.
For an unresolved attempt, read `BACKUP_PATH/upgrade-active` and the referenced
directory's `manifest`, `phase`, `failure` and `upgrade.log` where available. Read
these as text; do not execute or source them. The script refuses another upgrade
until that attempt has been resolved. Preserve the marker and backups while
investigating, block access using the recorded service manager, and check for any
still-running migration process before recovery. Reconcile external crontab edits rather than
blindly replacing them.

Existing maintenance mode, a pending migration, a concurrent upgrade or a legacy
`.old` directory also prevents an upgrade. Identify and resolve the underlying
state before trying again; do not delete recovery files merely to bypass a check.

After a reboot, power loss or forced termination, inspect the installation and
recorded recovery state manually. The active-attempt marker prevents a new run;
it does not prevent systemd or Monit from starting services after reboot.

## For contributors

The script is organized around preparation, backup, application replacement,
migration and restoration. `set_phase` records recovery boundaries; `on_exit`
uses them and the in-memory flags to recover or leave access blocked. Keep the
migration boundary recorded **before** launching `occ upgrade`. Application
rollback is only automatic before migration starts.

`occ` runs Nextcloud commands as `www-data`; `php_info` parses structured metadata
and validates releases/status. `stop_services`, `start_services` and
`wait_service` handle service control through `service_control`; `check_services`
validates the selected backend's prerequisites. `restore_cron` checks for
concurrent edits.
Keep ownership/deletion operations away from the user-data directory.

Run local checks from the repository:

```bash
bash -n nextcloud-upgrade.sh
shellcheck nextcloud-upgrade.sh
python3 tests/test_upgrade.py
```

Tests require Python 3, PHP 8.4 or newer with SimpleXML, and both archive toolsets.
Set `TEST_PHP=/path/to/php` to select another PHP executable. Tests use isolated
files and mocked Nextcloud, database and service commands; no root access,
network connection or running Nextcloud installation is required. Preserve
coverage of failures around backup, replacement, migration and restoration.

## License

[MIT](LICENSE.md). This project is not affiliated with or endorsed by Nextcloud.
