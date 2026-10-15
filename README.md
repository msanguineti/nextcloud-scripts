# Nextcloud Upgrade Script

Upgrade an archive-based Nextcloud installation from the command line. The
script downloads a stable release, backs up the database and configuration,
manages maintenance mode and services, and runs the upgrade. It keeps the old
application for recovery. Data backups and off-server copies are optional.

## Requirements

- A single Nextcloud installation on Linux with systemd.
- Nginx with PHP-FPM, or Apache with PHP-FPM or `mod_php`.
- MariaDB/MySQL or PostgreSQL, with the matching client and dump tools installed.
- Bash, GNU tools, PHP CLI with SimpleXML, `sudo`, `curl`, `pgrep`, `flock`,
  `findmnt`, `tar` and `bzip2`. Account cron management also needs `crontab`.
- Monit for the default service manager, or `SERVICE_MANAGER=systemd`.
- Space for the release, staged application and selected backups.

ZIP releases need `unzip`/`zipinfo`. Data backups need `rsync` and a destination
supporting Unix ownership, permissions, ACLs and extended attributes. SSH backup
destinations need `tar` and GNU `sync` on the remote server.

Use your provider's updater for Docker/AIO, Snap, appliances or distribution
packages. Clusters, remote web/PHP services, SQLite, Oracle, external configuration
directories and external/nested app directories are unsupported. Database
connections needing extra TLS options or custom PostgreSQL ports are unsupported.

Installation and backup directories must be existing absolute paths without
symlinks. The application directory cannot be a mount point or contain mounts.
External data directories may contain mounts. Application symlinks must be
relative links to files within the installation, outside user data; configuration
links must stay within `config/`. Directory links and broken links are unsupported.

## Installation

Download the archive and checksum from
[Releases](https://github.com/msanguineti/nextcloud-scripts/releases). For example:

```bash
release=v0.1.0
url="https://github.com/msanguineti/nextcloud-scripts/releases/download/$release"
curl --fail --location --remote-name "$url/nextcloud-upgrade-$release.tar.gz"
curl --fail --location --remote-name "$url/nextcloud-upgrade-$release.tar.gz.sha256"
sha256sum -c "nextcloud-upgrade-$release.tar.gz.sha256"
tar -xzf "nextcloud-upgrade-$release.tar.gz"
cd "nextcloud-upgrade-$release"
```

Verify the checksum succeeds before extracting. The package contains the script,
README and license. Alternatively, clone the repository:

```bash
git clone https://github.com/msanguineti/nextcloud-scripts.git
cd nextcloud-scripts
```

Install the script and create the default backup directory:

```bash
sudo install -o root -g root -m 0755 nextcloud-upgrade.sh /usr/local/sbin/nextcloud-upgrade
sudo install -d -o root -g root -m 0700 /var/backups/nextcloud
nextcloud-upgrade --version
```

To update, download a newer release or update your checkout, then repeat the
installation command. Release packages require neither Git nor Docker.

## Usage

Before upgrading:

- Check the target release's [requirements](https://docs.nextcloud.com/server/stable/admin_manual/installation/system_requirements.html)
  and [upgrade notes](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/upgrade.html),
  including CLI/web PHP versions, extensions and database compatibility.
- Check third-party app compatibility and disable incompatible apps.
- Confirm your data backup is current, or enable `BACKUP_DATA=yes`.
- Pause systemd timers, system cron entries and external CLI workers. The script
  manages account crontab entries only in cron mode. Stopping shared web/PHP
  services also interrupts other sites using them.

Find and install the next eligible stable update:

```bash
sudo nextcloud-upgrade
```

Review the versions, paths, services, background-job mode and backup settings at
confirmation. Enter `y` to proceed. Long preparation steps show updates every
15 seconds. After enabling maintenance, the script allows 30 seconds for active
requests before stopping services and taking backups.

To use a 60-second settling pause:

```bash
sudo env WAIT_AFTER_MAINTENANCE=60 nextcloud-upgrade
```

To select a maintenance release within your installed major version:

```bash
sudo nextcloud-upgrade 34.0.5
```

Use automatic discovery for major upgrades, after installing the latest
maintenance release of your current major version. Downgrades, skipped major
versions and automatic upgrades from non-stable channels are rejected.

After upgrading, check login, file upload/download, client sync and Administration
Overview. Confirm background jobs resume, re-enable compatible apps and complete
any recommended database maintenance before another major upgrade.

```bash
nextcloud-upgrade --help
nextcloud-upgrade --version
```

Coloured `[✓]`, `[!]` and `[x]` indicate success, warnings and errors. Use
`NO_COLOR=1` to disable colours. Redirected output has no colour escapes.

## Services and background jobs

Defaults are Monit, Nginx, PHP 8.4 FPM and `www-data`. Keep
`SERVICE_MANAGER=monit` when Monit supervises the selected services. Monit names
must match their systemd units. Services must already be running and, with
Monit, monitored. Start with maintenance mode disabled.

For Nginx/PHP-FPM managed directly by systemd:

```bash
sudo env SERVICE_MANAGER=systemd nextcloud-upgrade
```

For Apache with embedded PHP:

```bash
sudo env SERVICE_MANAGER=systemd WEB_SERVICE=apache2 PHP_SERVICE=none nextcloud-upgrade
```

Use `WEB_SERVICE=httpd` if that is your service name. For separate PHP-FPM, set
`PHP_SERVICE` to its unit name. Set `WEB_USER`, `WEB_GROUP` and `PHP_BIN` to match
your installation. In systemd mode, pause other supervisors or activation
mechanisms that could restart these services.

The script detects and preserves your background-job setting:

| Mode | During the upgrade |
| --- | --- |
| Cron | Suspends this installation's entries in `WEB_USER`'s crontab and restores them afterward. |
| WebCron | Maintenance mode and stopped web services prevent web-triggered jobs. The external schedule is unchanged. |
| AJAX | Maintenance mode and stopped web services prevent page-triggered jobs. |
| Disabled | Jobs remain disabled. |

[Cron is recommended by Nextcloud](https://docs.nextcloud.com/server/stable/admin_manual/configuration_server/background_jobs_configuration.html).
For systemd timers, system cron or other external schedulers, pause and resume
them yourself. You may also pause your WebCron provider to avoid failed requests
during downtime. Cron mode without an account crontab is allowed with a warning.

## Backups

Database and configuration backups are always created. **Data backup is off by
default.** Database settings and the data directory are read from Nextcloud.

To back up data and create a recovery bundle on a mounted destination:

```bash
sudo env BACKUP_PATH=/mnt/backups/nextcloud \
    BACKUP_DATA=yes BACKUP_ARCHIVE=yes nextcloud-upgrade
```

Create the destination first, owned by root and not writable by group or others.
It must be outside the installation and data directories. Check that a mounted
destination is actually mounted before running the script.

Local storage, including `/var/backups/nextcloud`, is supported. An off-server
copy provides protection against server or disk failure. Storage advisories do
not block the upgrade.

To send an additional recovery archive over SSH:

```bash
sudo env BACKUP_SSH_DEST=backup@backup-server:/srv/backups/nextcloud nextcloud-upgrade
```

Create the remote directory and configure noninteractive SSH access and a
verified host key for root. Use an SSH host alias for custom ports or IPv6.
Local backups are retained; the SSH copy must succeed before migration. Add
`BACKUP_DATA=yes` to include filesystem data.

Data copies are verified and can extend downtime substantially. Pause other
applications writing directly to the data directory. Allow space for a full
copy and, with `BACKUP_ARCHIVE=yes`, an additional tar bundle. Old backups are
not deleted automatically.

Data inside the installation keeps its permissions when moved to the new
application. The old application directory contains no separate data copy.
`BACKUP_DATA` includes nested mounts in an external data directory. External
storage connected through apps needs separate backups. Primary object storage
cannot use `BACKUP_DATA=yes`.

### Recovery files

Each confirmed run creates
`BACKUP_PATH/upgrade-SOURCE_VERSION-TIMESTAMP.RANDOM`, for example
`upgrade-34.0.4.1-20261015T120000Z.A1B2C3D4`.

| Item | Contents |
| --- | --- |
| `database.sql.gz` | Pre-upgrade database dump. |
| `config/`, `.htaccess`, `.user.ini` | Configuration and server overrides, when present. |
| `custom/` | Preserved apps, themes and selected files. |
| `data/` | Data copy, only with `BACKUP_DATA=yes`. |
| `backup.tar`, `backup.tar.sha256` | Optional recovery bundle and checksum. |
| `manifest`, `phase`, `failure`, `upgrade.log` | Versions, paths, background-job mode, status and diagnostic output, when available. |
| `cron.original`, `cron.disabled` | Account cron snapshots, when account cron is managed. |

The previous application remains beside the installation as `.old.upgrade-…`.
Bundles and SSH archives contain the backup set; keep the old application
separately. Files ending in `.part` or `.partial` are incomplete.

### Failed upgrades

- **Before database migration:** the script attempts to restore the original
  application and resume normal operation. Check the reported result.
- **After migration starts:** services remain stopped and maintenance enabled.
  Restore the matching database, configuration and application version. Restore
  data too if it changed or was lost.

Do not rerun blindly. Read `BACKUP_PATH/upgrade-active` and the referenced
recovery directory's `manifest`, `phase`, `failure` and `upgrade.log`. Resolve the
failure before removing the active marker. After a reboot, check that access is
blocked; the marker prevents another upgrade but does not stop services starting.

To inspect and extract a recovery bundle into an empty directory:

```bash
cd /path/to/upgrade-SOURCE_VERSION-TIMESTAMP.RANDOM
sha256sum -c backup.tar.sha256
tar -tf backup.tar
sudo tar --acls --xattrs --numeric-owner -xpf backup.tar -C /path/to/empty/restore-directory
```

Follow [Nextcloud's restore guide](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/restore.html).
Import SQL into a clean database and use the application version recorded in
`manifest`. Check ownership and restore compatible custom files before resuming
services and background jobs. If the data backup is outdated, follow the guide's
client-synchronization instructions. Configuration and SQL cannot recover lost
user files. Keep recovery files until the upgraded installation is verified.

## Custom files

The script preserves `config/`, `.htaccess`, `.user.ini`, custom themes,
additional direct-child app directories in `apps_paths`, and third-party apps
absent from the new release. Shipped app versions take precedence; removed
shipped apps are not carried forward. Check app/theme compatibility and review
custom `.htaccess` directives after upgrading.

For additional files, create `/root/nextcloud-preserve.txt`:

```text
# Paths relative to the Nextcloud installation
local-assets
my custom page.html
```

```bash
sudo env PRESERVE_PATHS_FILE=/root/nextcloud-preserve.txt nextcloud-upgrade
```

Use one existing file or directory per line, without quotes or wildcards. Hidden
files are included; blank lines and comments are ignored. Do not list data or
paths already preserved automatically. Absolute paths, overlapping paths,
paths through symlinks and conflicts with release content are rejected.

## Options

Pass settings with `sudo env NAME=value ... nextcloud-upgrade`.

| Setting | Default | Description |
| --- | --- | --- |
| `NEXTCLOUD_PATH` | `/var/www/nextcloud` | Installation directory. |
| `BACKUP_PATH` | `/var/backups/nextcloud` | Recovery backup destination. |
| `SERVICE_MANAGER` | `monit` | `monit` or `systemd`. |
| `WEB_SERVICE` | `nginx` | Web-server service name. |
| `PHP_SERVICE` | `php8.4-fpm` | PHP-FPM service, or `none` for embedded PHP. |
| `WEB_USER` | `www-data` | HTTP account and owner of the managed crontab. |
| `WEB_GROUP` | Primary group of `WEB_USER` | Application file group. |
| `PHP_BIN` | `php` | PHP CLI executable; an absolute path is recommended. |
| `BACKUP_DATA` | `no` | Copy and verify filesystem data (`yes`/`no`). |
| `BACKUP_ARCHIVE` | `no` | Create a tar bundle and checksum (`yes`/`no`). |
| `BACKUP_SSH_DEST` | Unset | Additional archive at `[user@]host:/absolute/directory`. |
| `PRESERVE_PATHS_FILE` | Unset | File listing extra installation-relative paths. |
| `WAIT_AFTER_MAINTENANCE` | `30` | Grace period before service shutdown; `0` skips it. |
| `WAIT_BEFORE_BACKUP` | `60` | Maximum wait for running cron/occ processes. |
| `WAIT_AFTER_SERVER_START` | `20` | Maximum wait for each service state change. |
| `DOWNLOAD_FORMAT` | `tar.bz2` | `tar.bz2` or `zip`. |
| `CHECKSUM_TYPE` | `sha256` | `sha256`, `sha512` or `md5`. |
| `NO_COLOR` | Unset | Disable terminal colours. |

Release checksums come from the archive's HTTPS server; they are not independent
signature verification. Prefer SHA256 or SHA512 over MD5.

## Contributing

On Debian 13, install the development tools and run all checks:

```bash
sudo apt update
sudo apt install git python3 shellcheck php8.4-cli php8.4-xml rsync tar bzip2 unzip
git clone https://github.com/msanguineti/nextcloud-scripts.git
cd nextcloud-scripts
TEST_PHP=/usr/bin/php8.4 bash .github/scripts/check.sh
```

Tests need PHP 8.4 or newer with SimpleXML. They use temporary files and simulated
services, databases and GitHub requests; no root or live Nextcloud server is
needed. The runner uses two workers and reports slow tests. Use `TEST_JOBS=1`
for sequential execution. To run one test:

```bash
TEST_PHP=/usr/bin/php8.4 python3 -B -m unittest discover -s tests -k test_zip_success -v
```

### Development container

With Docker available, build once and run checks without installing the project
dependencies on your system:

```bash
docker build -t nextcloud-script-dev .devcontainer
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  nextcloud-script-dev
```

Rebuild after changing the Dockerfile. The source mount is read-only and the
container is removed afterward. Add `-e TEST_JOBS=1` before the image name for
sequential tests.

### Code and releases

`nextcloud-upgrade.sh` contains the upgrade and recovery functions. `set_phase`
records recovery state; `on_exit` handles failures and interruption. Database
migration marks the boundary for manual recovery.

CI checks PRs and pushes to `main`. To release, update `SCRIPT_VERSION` in a PR
and merge it. Run **Actions → Prepare release → Run workflow** on **main**.
Review the draft's archive, checksum and release notes, then **Publish**. An
existing tag or release for that version causes an error. Immutable releases
are supported.

Protect `main` with required PRs and passing `Checks`, and restrict the `release`
environment to `main`. If `v*` tag creation is restricted to automation, install
a GitHub App with **Contents: read and write**, allow it in the tag ruleset, and
set `RELEASE_APP_CLIENT_ID` (environment variable) and `RELEASE_APP_PRIVATE_KEY`
(environment secret) in GitHub's `release` environment.

To build a local package:

```bash
bash .github/scripts/package-release.sh v0.1.0
```

Downloads are written to `dist/`.

## License

[MIT](LICENSE.md). Not affiliated with or endorsed by Nextcloud.
