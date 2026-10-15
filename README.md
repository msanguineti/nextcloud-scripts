# Nextcloud Upgrade Script

Upgrade an existing Nextcloud installation from the command line. The script
downloads a stable release, backs up the database and configuration, manages
maintenance mode and services, and runs the upgrade. It retains the previous
application files for recovery. Data backups and off-server copies are optional.

## Requirements

- One archive-based Nextcloud installation on a Linux server with systemd.
- Nginx with PHP-FPM, or Apache with PHP-FPM or `mod_php`.
- MariaDB/MySQL or PostgreSQL, with the corresponding database client and dump
  tools installed (`mariadb`/`mariadb-dump`, `mysql`/`mysqldump`, or `psql`/`pg_dump`).
- Bash, GNU command-line tools, PHP CLI with SimpleXML, `sudo`, `curl`,
  `crontab`, `pgrep`, `flock`, `findmnt`, `tar` and `bzip2`.
- Monit when using the default service manager; otherwise select `systemd`.
- Space for the downloaded release, staged application and selected backups.

ZIP downloads also need `unzip`/`zipinfo`; data backups need `rsync` and storage
that preserves Unix ownership, permissions, ACLs and extended attributes. SSH
backup destinations need `tar` and GNU `sync` on the remote server.

Use the installation provider's updater for Docker/AIO, Snap, appliances and
distribution packages. This script does not coordinate clusters or remote
web/PHP services, or support SQLite, Oracle, external configuration directories
or external/nested app directories. Database connections requiring additional
TLS options or custom PostgreSQL ports need a separate upgrade procedure.

Installation and backup paths must be existing absolute paths without symlinks.
The application directory cannot be a mount point or contain mounts; data inside
the installation must also be free of mounts. Relative file symlinks within the
application are allowed, except links into user data or links leaving `config/`.
Directory links, broken links and links outside the installation are unsupported.

## Installation

Download the archive and checksum from
[Releases](https://github.com/msanguineti/nextcloud-scripts/releases). For example,
for script release `v0.1.0`:

```bash
release=v0.1.0
url="https://github.com/msanguineti/nextcloud-scripts/releases/download/$release"
curl --fail --location --remote-name "$url/nextcloud-upgrade-$release.tar.gz"
curl --fail --location --remote-name "$url/nextcloud-upgrade-$release.tar.gz.sha256"
sha256sum -c "nextcloud-upgrade-$release.tar.gz.sha256"
tar -xzf "nextcloud-upgrade-$release.tar.gz"
cd "nextcloud-upgrade-$release"
```

Choose an available release and check that verification succeeds before
extracting. The package contains the script, README and license.

Alternatively, clone the repository:

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

To update the script, download a newer release or update your checkout, then
repeat the installation command. Git and Docker are not needed to use a release
package.

## Usage

Before upgrading:

- Check the target release's [system requirements](https://docs.nextcloud.com/server/stable/admin_manual/installation/system_requirements.html)
  and [upgrade notes](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/upgrade.html).
  Check both CLI and web PHP, including extensions, and the database version.
- Check third-party app compatibility and disable those apps before upgrading.
- Confirm that your data is backed up, or enable `BACKUP_DATA=yes` below.
- Pause systemd timers, system cron entries and external workers yourself. The
  script suspends only this installation's entries in `WEB_USER`'s account
  crontab. Stopping shared web/PHP services also interrupts their other sites.

Find and install the next eligible stable update:

```bash
sudo nextcloud-upgrade
```

The script prepares the release, displays the selected versions and settings,
then asks for confirmation. Enter `y` to proceed. Services and maintenance mode
are changed only after confirmation.

To select a maintenance release within the installed major version:

```bash
sudo nextcloud-upgrade 34.0.1
```

For major upgrades, use automatic discovery after installing the latest
maintenance release of the current major version. Downgrades and skipped major
versions are rejected. Automatic discovery uses the stable update channel.

After upgrading, check the web interface, Administration Overview, a file
upload/download and client sync. Confirm that jobs resume, re-enable compatible
apps, and follow any recommended database maintenance. Let background migrations
finish before another major upgrade.

```bash
nextcloud-upgrade --help
nextcloud-upgrade --version
```

`--version` reports the script version; an argument such as `34.0.1` selects a
Nextcloud version. Coloured `[✓]`, `[!]` and `[x]` mark success, warnings and
errors. Set `NO_COLOR=1` to disable colours; redirected output contains no colour
escapes.

## Service settings

Defaults are Monit, Nginx, PHP 8.4 FPM and the `www-data` account. Keep
`SERVICE_MANAGER=monit` when Monit supervises these services, so it does not
restart them during the upgrade. Monit service names must match the systemd
units. Selected services must already be running and, with Monit, monitored.

For Nginx/PHP-FPM managed directly by systemd:

```bash
sudo env SERVICE_MANAGER=systemd nextcloud-upgrade
```

For Apache with embedded PHP:

```bash
sudo env SERVICE_MANAGER=systemd WEB_SERVICE=apache2 PHP_SERVICE=none nextcloud-upgrade
```

Use `WEB_SERVICE=httpd` if that is your service name. With PHP-FPM, set
`PHP_SERVICE` to its unit name instead of `none`. Select the HTTP account with
`WEB_USER`/`WEB_GROUP` and the CLI executable with `PHP_BIN`. These settings
select existing services and accounts; they do not configure them. In systemd
mode, other supervisors or activation mechanisms must not restart the services.

## Backup settings

**Data backup is off by default.** The script reads the data directory and
database settings from Nextcloud's configuration. Enable data backup if your
existing backup system does not cover the filesystem data directory.

Save database, configuration and data backups to a mounted backup destination,
with a tar bundle for convenient recovery:

```bash
sudo env BACKUP_PATH=/mnt/backups/nextcloud \
    BACKUP_DATA=yes BACKUP_ARCHIVE=yes nextcloud-upgrade
```

Create the destination first, owned by root and not writable by group or others.
It must be outside the installation and data directories. If it is on a mounted
filesystem, confirm that the mount is present before running the script.

Local destinations, including `/var/backups/nextcloud`, are allowed. An off-server
copy offers better protection against server or disk failure. Local-storage
advisories do not block an upgrade.

To send an additional backup archive over SSH:

```bash
sudo env BACKUP_DATA=yes \
    BACKUP_SSH_DEST=backup@backup-server:/srv/backups/nextcloud \
    nextcloud-upgrade
```

Create the remote directory and configure noninteractive SSH access and a
verified host key for the root user running the script. Use an SSH host alias
for custom ports or IPv6. The SSH copy must succeed before migration; local
backups are retained too.

Backups are taken while maintenance mode is active and services and scheduled
jobs are stopped. Data is copied and checked with `rsync`; this can substantially
extend downtime. Stop other applications that write directly to the data during
the backup. These are full backups, and keeping a tar bundle requires additional
space. Older backups are not deleted automatically.

Data inside the installation is moved into the new application without changing
its permissions. The retained old application contains **no separate data copy**.
`BACKUP_DATA` covers the configured filesystem data directory, including nested
mounts in an external data directory. Primary object storage and storage attached
through Nextcloud apps need their own backups; primary object storage cannot
use `BACKUP_DATA=yes`.

## Custom files

The script preserves `config/`, `.htaccess`, `.user.ini`, custom themes,
additional direct-child app directories configured in `apps_paths`, and
third-party apps missing from the new release. New shipped app versions take
precedence; removed shipped apps are not carried forward. Preserved apps and
themes still need to be compatible with the target release. Review custom
`.htaccess` directives afterward; Nextcloud may regenerate that file.

For other files, create a list such as `/root/nextcloud-preserve.txt`:

```text
# Paths relative to the Nextcloud installation
local-assets
my custom page.html
```

```bash
sudo env PRESERVE_PATHS_FILE=/root/nextcloud-preserve.txt nextcloud-upgrade
```

Use one existing file or directory per line, without quotes or wildcards.
Directories include hidden files. Empty lines and comments are ignored.
Do not list data or paths already preserved automatically. Absolute paths,
overlaps and paths traversing symlinks are rejected. Custom files, apps and
themes must not overwrite release content; conflicts stop preparation for review.

## Options

Pass settings with `sudo env NAME=value ... nextcloud-upgrade`.

| Setting | Default | Description |
| --- | --- | --- |
| `NEXTCLOUD_PATH` | `/var/www/nextcloud` | Installation directory. |
| `BACKUP_PATH` | `/var/backups/nextcloud` | Recovery backup directory. |
| `SERVICE_MANAGER` | `monit` | `monit` or `systemd`. |
| `WEB_SERVICE` | `nginx` | Web-server service name. |
| `PHP_SERVICE` | `php8.4-fpm` | PHP-FPM service, or `none` for embedded PHP. |
| `WEB_USER` | `www-data` | Non-root HTTP account; also selects its crontab. |
| `WEB_GROUP` | Primary group of `WEB_USER` | Application file group, readable by web/PHP processes. |
| `PHP_BIN` | `php` | CLI executable; an absolute path is recommended. |
| `BACKUP_DATA` | `no` | Copy and verify the filesystem data directory (`yes`/`no`). |
| `BACKUP_ARCHIVE` | `no` | Also create `backup.tar` and a SHA256 checksum (`yes`/`no`). |
| `BACKUP_SSH_DEST` | Unset | Additional archive at `[user@]host:/absolute/directory`. |
| `PRESERVE_PATHS_FILE` | Unset | List of additional installation-relative paths. |
| `WAIT_BEFORE_BACKUP` | `60` | Seconds to wait for existing cron/occ processes. |
| `WAIT_AFTER_SERVER_START` | `20` | Seconds to wait for each service state change. |
| `DOWNLOAD_FORMAT` | `tar.bz2` | `tar.bz2` or `zip`. |
| `CHECKSUM_TYPE` | `sha256` | `sha256`, `sha512` or legacy `md5`; prefer SHA256/SHA512. |
| `NO_COLOR` | Unset | Set to disable terminal colours. |

Release checksums are fetched from the same HTTPS server as the archive; they
are not independent signature verification.

## Backups and troubleshooting

Each confirmed attempt creates `BACKUP_PATH/upgrade-TIMESTAMP.RANDOM`. Its
contents include:

| Item | Contents |
| --- | --- |
| `database.sql.gz` | Compressed database dump. |
| `config/`, `.htaccess`, `.user.ini` | Pre-upgrade configuration, when present. |
| `custom/` | Preserved apps, themes and additional files. |
| `data/` | Verified data copy, only with `BACKUP_DATA=yes`. |
| `backup.tar`, `backup.tar.sha256` | Optional recovery bundle and checksum. |
| `manifest`, `phase`, cron snapshots, `upgrade.log` | Versions, paths, recovery state and migration output. |

The old code is retained beside the installation as `.old.upgrade-…`. Recovery
bundles and SSH archives contain the pre-migration backup set, not the full old
code or later logs. Files ending in `.part` or `.partial` are incomplete.

If an upgrade fails:

- **Before database migration:** the script attempts to restore the original
  application, services, cron and maintenance state. Check the reported result.
- **After migration starts:** access is kept blocked for manual recovery.
  Restore a matching database, configuration, data and application version;
  replacing only the code cannot undo a database migration.

Read `BACKUP_PATH/upgrade-active` and the referenced directory's `manifest`,
`phase`, `failure` and `upgrade.log` as text. Resolve the recorded failure before
removing the active marker or retrying. After a reboot or forced termination,
check services and block access if needed: the marker prevents another upgrade,
but does not prevent services from starting.

To inspect and extract a local recovery bundle into an empty directory:

```bash
cd /path/to/upgrade-TIMESTAMP.RANDOM
sha256sum -c backup.tar.sha256
tar -tf backup.tar
sudo tar --acls --xattrs --numeric-owner -xpf backup.tar -C /path/to/empty/restore-directory
```

Follow [Nextcloud's restore instructions](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/restore.html),
using the version recorded in `manifest` and importing SQL into a clean database.
Restore custom files to their original relative paths and check ownership before
starting services. If `data/` is absent, use your existing data backup as needed;
database and configuration backups cannot recover lost user files. For outdated
data backups, follow the guide's client-synchronization advice.

Remove obsolete backups and old application directories after verifying the
upgrade. Keep a suitable recovery set.

## Contributing

Tests need Python 3, PHP 8.4 or newer with SimpleXML, ShellCheck, `rsync`, GNU
tools and both archive toolsets. On Debian 13:

```bash
sudo apt update
sudo apt install git python3 shellcheck php8.4-cli php8.4-xml rsync tar bzip2 unzip
git clone https://github.com/msanguineti/nextcloud-scripts.git
cd nextcloud-scripts
TEST_PHP=/usr/bin/php8.4 bash .github/scripts/check.sh
```

The check command runs shell validation and all tests. Tests use temporary files
and simulated services, databases and GitHub requests; no root access, Docker or
live Nextcloud server is needed. CI runs the same checks on PRs and `main` pushes.

The upgrade flow is in `nextcloud-upgrade.sh`. `set_phase` records progress and
`on_exit` handles recovery; automatic rollback ends when database migration
starts. Data handling is separate from application ownership changes.

### Releases

Include the `SCRIPT_VERSION` update in your PR. After merging, open
**Actions → Prepare release → Run workflow**, select **main**, and run it.
The workflow checks that the version is unused, tests the selected commit, then
creates its version tag and a draft with the archive, checksum and release notes.
For example, script version `0.1.0` creates tag `v0.1.0`. An existing tag or
release causes an error; neither is overwritten. PR merges and tag pushes do
not prepare releases.

Review the draft under **Releases**, then click **Publish**. Release immutability
can stay enabled. If preparation fails after creating a tag, inspect and remove
only the incomplete, unpublished draft/tag before retrying.

Configure protection rules in GitHub's UI. For `main`, require a PR and passing
`Checks`, and block force pushes and deletion. Restrict the `release` environment
to `main`. The workflow uses `GITHUB_TOKEN` by default and never updates `main`.
For automation-only `v*` tag creation, allow a dedicated GitHub App in the tag
ruleset, install it on this repository with **Contents: read and write**, and set
`RELEASE_APP_CLIENT_ID` (variable) and `RELEASE_APP_PRIVATE_KEY` (secret) in the
`release` environment. The workflow then uses that App for tag/release creation.

For a local package without creating a GitHub release:

```bash
bash .github/scripts/package-release.sh v0.1.0
```

The archive and checksum are written to `dist/`.

## License

[MIT](LICENSE.md). Not affiliated with or endorsed by Nextcloud.
