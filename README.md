# Nextcloud Upgrade Script

Upgrade an existing Nextcloud installation from the command line. The script
finds the next eligible stable release, downloads and checks it, backs up the
database and configuration, and installs the update while managing maintenance
mode, services and scheduled jobs. Data backups, archive packaging and an off-site
copy over SSH are optional.

It keeps the previous application files and recovery backups so that a failed
upgrade can be investigated and recovered. Downloads and extraction happen on
disk before the installation is taken offline.

## Requirements

The script supports one archive-based Nextcloud installation on a systemd Linux
server with:

- Bash, GNU command-line utilities and PHP CLI with SimpleXML.
- A non-root HTTP account for Nextcloud commands and jobs, selected with
  `WEB_USER` (default `www-data`).
- A web server with a systemd service unit: Nginx with PHP-FPM, or Apache 2.4
  with PHP-FPM or `mod_php`. Managed services must already be running.
- Monit, when using the default service manager. Its service names must match
  the systemd unit names, and all selected services must already be monitored.
- MariaDB/MySQL or PostgreSQL. Database settings are read from Nextcloud.
- A filesystem data directory inside or outside the installation; its location
  is read from Nextcloud's `datadirectory` configuration.
- Enough disk space and inodes for the downloaded release, extracted files,
  staged installation and recovery backups.

The required utilities include `sudo`, `curl`, `systemctl`, `crontab`, `pgrep`,
`flock`, `findmnt`, `gzip`, and checksum tools. Monit is only required when
`SERVICE_MANAGER=monit`; select `systemd` to manage services directly. The default
archive format uses `tar` and `bzip2`; ZIP requires `unzip` and `zipinfo`.
Data backups require `rsync` and a destination filesystem that can preserve Unix
ownership, permissions, ACLs and extended attributes. Archive packaging requires
GNU `tar` and `sha256sum`; SSH copies require local `ssh` and GNU `tar`, and `tar`
and GNU `sync` on the destination server.
For MariaDB/MySQL, install `mariadb` and `mariadb-dump`, or their `mysql` and `mysqldump` equivalents.
For PostgreSQL, install `psql` and `pg_dump`. The script checks required commands
before upgrading.

This follows Nextcloud's [manual archive upgrade method](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/manual_upgrade.html).
For Docker/AIO, Snap, pre-built appliances or distribution packages, Nextcloud's
[upgrade overview](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/upgrade.html)
directs administrators to the method provided by that installation. This script does not coordinate
multiple application servers, remote web/PHP services or external workers. It
supports MariaDB/MySQL and PostgreSQL, not SQLite or Oracle. Database access uses
the configured host and credentials with standard client settings; deployments
requiring extra connection options, client certificates or custom PostgreSQL
ports need separate review before use.

Installation and backup paths must be existing, canonical absolute paths without
symlinks. The backup directory must be root owned and not writable by group or
others. Relative application symlinks must resolve to regular files inside the
installation, outside user data; links inside `config/` must stay inside it.
Directory links, broken/external links and application mounts are unsupported.
Internal data directories are excluded from application file checks, but must
not contain mounts because they are moved during replacement. External configuration
directories and external or nested custom app directories are not supported.

**Data backup is off by default.** Use your existing backup system, or enable
`BACKUP_DATA=yes` to save the filesystem data directory alongside the database and
configuration. Prefer backup storage off the server. A local destination is
allowed and useful for upgrade recovery, but shares the server's failure risks.
Independently scheduled database/server and data backups may not represent the
same point in time.

When data appears to be on local storage and this script's data backup is off,
it prints a short suggestion to keep a separate data backup if your existing
system does not already cover it. Storage checks are best-effort; these notes add no prompts
and do not block an upgrade.

## Installation

### Download a release

Download the `.tar.gz` package and matching `.sha256` file from
[GitHub Releases](https://github.com/msanguineti/nextcloud-scripts/releases).
The package contains the script, this README and the license. Git and Docker are
not required to install or run it; the server dependencies listed above still apply.

For example, to download script release `v0.1.0` once published:

```bash
curl --fail --location --remote-name https://github.com/msanguineti/nextcloud-scripts/releases/download/v0.1.0/nextcloud-upgrade-v0.1.0.tar.gz
curl --fail --location --remote-name https://github.com/msanguineti/nextcloud-scripts/releases/download/v0.1.0/nextcloud-upgrade-v0.1.0.tar.gz.sha256
sha256sum -c nextcloud-upgrade-v0.1.0.tar.gz.sha256
tar -xzf nextcloud-upgrade-v0.1.0.tar.gz
cd nextcloud-upgrade-v0.1.0
```

Choose the desired version from Releases and substitute its tag in these commands.
Check that the checksum succeeds before extracting. It detects download corruption;
it is fetched from the same release and is not an independent signature.

Install the script and create its default backup directory:

```bash
sudo install -o root -g root -m 0755 nextcloud-upgrade.sh /usr/local/sbin/nextcloud-upgrade
sudo install -d -o root -g root -m 0700 /var/backups/nextcloud
nextcloud-upgrade --version
```

### Clone instead

You can also clone the repository on the server that runs Nextcloud:

```bash
git clone https://github.com/msanguineti/nextcloud-scripts.git
cd nextcloud-scripts
```

Then use the same installation commands above, or run `sudo ./nextcloud-upgrade.sh`
from the checkout. To update an installed script, download and verify a newer
release (or update your checkout), review its changes, and repeat the script
installation command. Replacing the script does not itself upgrade Nextcloud.

### Prepare the server

Before the first upgrade, confirm that your installation matches the requirements,
review the target Nextcloud release notes and app compatibility, disable
third-party apps as directed by Nextcloud, and verify that your data backups are
available or enable this script's data backup. Schedule an interruption to web
access and background jobs. Stopping a selected service affects every site or PHP
pool that it hosts.

The script manages Nextcloud entries in `WEB_USER`'s account crontab. Pause any
other job launchers yourself before running it: systemd timers, `/etc/cron.d`
jobs, remote cron services and dedicated workers are not suspended automatically.
An absent account crontab is allowed and is left absent. Nextcloud documents
several [background-job methods](https://docs.nextcloud.com/server/stable/admin_manual/configuration_server/background_jobs_configuration.html);
choose the shutdown procedure that matches yours, and resume those launchers
after verification.

## Running an upgrade

To find and install the next eligible stable update:

```bash
sudo nextcloud-upgrade
```

To see usage and available settings without starting preparation or requiring
root access:

```bash
nextcloud-upgrade --help
nextcloud-upgrade --version
```

`--version` reports the version of this upgrade script; a positional version such
as `34.0.1` selects a Nextcloud release to install.

The script checks the installation, downloads and validates the release, stages
the new files, and checks database access and backup capacity. It then displays
the versions, selected account/PHP, services, data and backup settings, and asks
for confirmation. Enter `y` or `Y` to continue; any other answer cancels.
Cancellation at this prompt leaves the
installation running, although downloads and preparation have already occurred.

Progress messages pair text and phase labels with `[i]` for information, `[✓]`
for completed checks, `[!]` for advisories and `[x]` for errors. In a terminal,
these appear in cyan, green, yellow and red respectively. Questions use a blue
`[?]`, including the upgrade confirmation. Redirected output keeps
the symbols without colour escape codes. Set `NO_COLOR=1` to disable terminal
colours, for example `sudo env NO_COLOR=1 nextcloud-upgrade`.

After confirmation, the script enables maintenance mode, suspends this
installation's cron entries, stops the web server and any configured separate
PHP-FPM service through the selected manager, waits for existing Nextcloud CLI
jobs to finish, and creates the recovery backups. It then replaces the
application, runs `occ upgrade`, verifies the installed version and
database migration state, and restores services and the original cron schedule.
The completion summary gives elapsed time, the maintenance window and the exact
recovery, previous-code and optional archive locations.

You can also supply a specific stable release within the currently installed
major version. For example, when upgrading 34.0.0 to 34.0.1:

```bash
sudo nextcloud-upgrade 34.0.1
```

Use automatic discovery for major upgrades, after installing the latest
maintenance release of the current major version. Downgrades and upgrades that
skip a major version are rejected. Automatic discovery requires the stable update
channel; Nextcloud's release requirements and compatibility guidance still apply.

After the script finishes, open Nextcloud, check Administration Overview, test a
file upload/download and client sync, and confirm that background jobs resume.
Re-enable compatible third-party apps after verification. Before another major
upgrade, allow any background migrations to finish. The script's local checks
do not replace verification through your web interface.

Review the target release's critical changes and any long-running database
maintenance recommended by Administration Overview. `occ upgrade` performs
Nextcloud's upgrade repairs; the script does not automatically run every optional
repair, index-building or migration command.

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
| `WEB_USER` | `www-data` | Non-root HTTP/Nextcloud account; also selects the account crontab and running CLI jobs to inspect. |
| `WEB_GROUP` | Primary group of `WEB_USER` | Group applied to staged application files; select a group readable by the web/PHP processes. |
| `PHP_BIN` | `php` | PHP CLI executable. An absolute path avoids ambiguity between installed PHP versions. |
| `BACKUP_DATA` | `no` | `yes` copies and verifies the configured filesystem data directory before migration. |
| `BACKUP_ARCHIVE` | `no` | `yes` also creates `backup.tar` and its SHA256 checksum inside the attempt's backup directory. |
| `BACKUP_SSH_DEST` | Unset | Optional extra copy to `[user@]host:/absolute/directory` as a private archive, before migration. |
| `SERVICE_MANAGER` | `monit` | Service control backend: `monit` or `systemd`. |
| `WEB_SERVICE` | `nginx` | Web-server systemd unit name; also the Monit name when selected. |
| `PHP_SERVICE` | `php8.4-fpm` | Separate PHP-FPM service name, or `none` for PHP embedded in the web server. |
| `PRESERVE_PATHS_FILE` | Unset | Optional text file listing extra installation-relative files or directories to transfer. See below. |
| `WAIT_BEFORE_BACKUP` | `60` | Maximum seconds to wait for existing Nextcloud cron/occ processes after services stop. |
| `WAIT_AFTER_SERVER_START` | `20` | Maximum seconds to poll each service state after its start/stop command returns. |
| `DOWNLOAD_FORMAT` | `tar.bz2` | Release archive format: `tar.bz2` or `zip`. |
| `CHECKSUM_TYPE` | `sha256` | Checksum: `sha256`, `sha512`, or legacy `md5`. Prefer SHA256 or SHA512. |
| `NO_COLOR` | Unset | Set to disable terminal colours. |

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

### Choosing the web server

`WEB_SERVICE` is the actual service name, not a web-server installation option.
The default is `nginx` with a separate `php8.4-fpm` service. For Apache with
PHP-FPM on Debian/Ubuntu:

```bash
sudo env WEB_SERVICE=apache2 PHP_SERVICE=php8.4-fpm nextcloud-upgrade
```

For Apache with `mod_php`, PHP runs inside Apache, so select only its service:

```bash
sudo env WEB_SERVICE=apache2 PHP_SERVICE=none nextcloud-upgrade
```

Use `WEB_SERVICE=httpd` instead when that is your actual service name. These
settings can be combined with `SERVICE_MANAGER=systemd`. Select the actual HTTP
account and PHP executable separately. For example, on a host using `apache`
and `httpd`:

```bash
sudo env SERVICE_MANAGER=systemd WEB_SERVICE=httpd PHP_SERVICE=php-fpm \
    WEB_USER=apache WEB_GROUP=apache PHP_BIN=/usr/bin/php nextcloud-upgrade
```

Changing these options does not install or configure the web server. The selected
account must already run Nextcloud and be able to read its configuration and data.

`PHP_SERVICE=none` explicitly means there is no separate PHP runtime to manage.
It does not detect the PHP setup. Only the web service is then checked, stopped
and started, including during recovery; restarting it also replaces its embedded
PHP workers and OPcache. Nginx requires a separate PHP-FPM service, so the
combination `WEB_SERVICE=nginx PHP_SERVICE=none` is rejected. Keep any separate
runtime configured so existing PHP requests cannot continue during replacement.

For a different PHP-FPM service and a longer service-state polling timeout:

```bash
sudo env PHP_SERVICE=php8.3-fpm WAIT_AFTER_SERVER_START=60 nextcloud-upgrade
```

CLI and web PHP must meet the requirements of both the installed and target
Nextcloud versions, including required extensions. `PHP_SERVICE` selects the
service; `PHP_BIN` selects the command-line executable. Use matching PHP versions
and check both configurations, since their extension and memory settings can
differ. The script checks CLI PHP against the downloaded release; it does not
validate the web runtime, operating system or database version automatically.

To use ZIP and SHA512:

```bash
sudo env DOWNLOAD_FORMAT=zip CHECKSUM_TYPE=sha512 nextcloud-upgrade
```

Checksum verification checks the downloaded archive against a checksum fetched
from the same HTTPS release server; it is not an independent signature check.
Cron entries are restored exactly, including their schedules and disabled entries.
Only recognized entries for this installation's `cron.php` are suspended;
unrecognized invocations require review before the script proceeds.

## Data backups and destinations

Every confirmed attempt gets one directory under `BACKUP_PATH`, named
`upgrade-TIMESTAMP.RANDOM`. It holds the configuration, compressed database dump,
preserved custom components, optional data copy, and the details needed to
identify the matching installation and versions. Files from previous attempts
are retained.

For a local backup, including data and an optional restore bundle:

```bash
sudo env BACKUP_PATH=/var/backups/nextcloud \
    BACKUP_DATA=yes BACKUP_ARCHIVE=yes nextcloud-upgrade
```

You can point `BACKUP_PATH` at an already mounted backup destination instead.
Prepare an existing directory owned by root and not writable by group or others:

```bash
sudo env BACKUP_PATH=/mnt/backup-server/nextcloud \
    BACKUP_DATA=yes BACKUP_ARCHIVE=yes nextcloud-upgrade
```

Ensure that a required backup mount is actually mounted before running the
script; an ordinary directory with that name is also a valid local destination.
The script checks writability and capacity but cannot know whether a configured
directory is meant to be a mount. You may also use the local directory or bundle
with your own system for moving backups elsewhere.

Alternatively, keep the local recovery files and send an additional archive over
SSH:

```bash
sudo env BACKUP_DATA=yes \
    BACKUP_SSH_DEST=backup@backup-server:/srv/backups/nextcloud \
    nextcloud-upgrade
```

The remote directory must already exist and be writable by the SSH account.
Configure key/agent authentication and the server's verified host key for the root
user that runs this script. SSH uses noninteractive authentication and strict
host-key checking. Use an SSH host alias for custom ports, keys or IPv6 addresses.
The script checks access before confirmation, then streams a uniquely named
`upgrade-TIMESTAMP.RANDOM.tar` before changing the application or database. The
remote archive is created with mode `600`, checked for readability by `tar`,
renamed from `.tar.partial`, and flushed to storage. A selected SSH copy is
required to succeed before the upgrade continues. Local recovery files remain
available; SSH transfer still requires enough space for those local backups.

Data is copied only after maintenance mode is enabled, cron and services are
stopped, and existing Nextcloud CLI jobs have finished. The database dump,
configuration and data copy share that stopped window, following
[Nextcloud's backup guidance](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/backup.html).
The data copy uses `rsync` to preserve metadata and symlinks without following
them, then compares file contents by checksum and checks metadata. Detected
differences or copy errors stop the upgrade before migration. Nested mounts
inside an external data directory are included. Other applications writing
directly to the data directory must also be stopped by the administrator.

Data inside the installation is moved into the replacement application by
rename, keeping its ownership and permissions. It is excluded from application
copying and recursive permission changes. The retained old application then
contains code and configuration, **not another data copy**. With `BACKUP_DATA=no`,
neither an internal nor an external data directory is copied into the backups.

Copying and checksum verification read all data and can extend downtime
substantially. Free space and inodes are checked before maintenance and again
after services stop. `BACKUP_ARCHIVE=yes` keeps the directory copy as well as the
uncompressed tar bundle, so it needs additional space. These are full backups;
there is no automatic retention or deletion of older attempts.

Primary object-storage deployments cannot use `BACKUP_DATA=yes`: a local data
folder does not contain the primary object-store contents. Remote/external
storage connected through Nextcloud apps also needs its own backup. This option
covers the configured filesystem data directory.

## Custom apps, themes and configuration

The script transfers the complete `config/` directory, `.htaccess`, `.user.ini`,
custom themes, and additional direct-child app directories configured in
`apps_paths`. Apps in the old `apps/` directory are copied when absent from the
new release, unless their metadata marks them as shipped apps. When an app is
included in the new release, the new release's copy takes precedence.

Nextcloud's [upgrade guidance](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/upgrade.html#prerequisites)
says to check third-party app compatibility and disable those apps before the
upgrade; re-enable compatible apps afterward. Its
[manual upgrade guide](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/manual_upgrade.html)
instructs administrators to transfer custom apps and themes, noting that themes
may need modifications after upgrading. Copying files does not establish
compatibility: use a supported app version and review or adapt custom themes.

Nextcloud also [warns against custom app directory conflicts](https://docs.nextcloud.com/server/stable/admin_manual/apps_management.html#using-custom-app-directories).
This script stops before maintenance if an additional app directory or custom
theme would collide with release content, or an app in an additional app
directory has the same directory name as an app shipped by the new release.
Stopping on these conflicts is this script's policy; the cited guides do not
provide an automatic merge procedure. Review the conflicting component and its
configuration before retrying. The script does not rename apps or themes or
rewrite `apps_paths` to resolve conflicts.

Other custom files are retained in the old installation but are not automatically
transferred.

To transfer extra files or directories, create a plain text list, for example
`/root/nextcloud-preserve.txt`:

```text
# Paths relative to NEXTCLOUD_PATH
local-assets
lib/local-settings.json
my custom page.html
```

Then run:

```bash
sudo env PRESERVE_PATHS_FILE=/root/nextcloud-preserve.txt nextcloud-upgrade
```

Each entry names an existing regular file or directory inside the installation.
Directories include their contents, including hidden files. Use one literal path
per line, without quotes or a trailing slash. Spaces are supported; wildcards,
shell variables and commands are not expanded. Empty lines and lines beginning
with `#` are ignored. A list containing only comments or empty lines keeps the
default preservation behavior.

The script prints the selected paths before asking to proceed. It refuses missing
paths, absolute paths, `.` or `..` components, duplicates and overlapping entries.
Do not list paths already transferred automatically, such as `custom_apps/`
or a custom theme. Configuration, `.htaccess` and `.user.ini` are handled
separately. The `data/` path and any configured internal data directory are
reserved: use `BACKUP_DATA` for data backups. Custom preservation does not copy
files from outside the installation.

Extra files and directories must not overwrite anything supplied by the new
release. The same protection applies to automatically transferred custom themes
and additional app directories. A conflict stops the upgrade before maintenance
mode so you can review it; this option cannot carry local modifications to
shipped code into the new version. The script checks the source tree again after
stopping services and refuses unsafe links, mounts and special files. A selected
path must not itself traverse a symlink; safe internal file links inside a
selected directory are allowed.

Ownership and permissions are applied only to the staged application:
`WEB_USER:WEB_GROUP`, directories `750`, and files `640`. Symlink targets are not
followed by the ownership operation; user-data permissions are retained.

## Backups and troubleshooting

Each confirmed attempt creates a unique directory under `BACKUP_PATH`, named
`upgrade-TIMESTAMP.RANDOM`. After backup, its main contents are:

| Item | Contents |
| --- | --- |
| `database.sql.gz` | Validated compressed database dump. |
| `config/` | Complete configuration directory, including extra fragments. |
| `.htaccess`, `.user.ini` | Pre-upgrade root configuration files, when present. |
| `custom/` | Preserved apps, themes and extra files under their installation-relative paths, when present. |
| `data/` | Verified filesystem data copy, only with `BACKUP_DATA=yes`. |
| `backup.tar`, `backup.tar.sha256` | Optional bundle and checksum, only with `BACKUP_ARCHIVE=yes`. |
| `manifest`, `preserved-paths`, `phase`, cron snapshots | Installation/version details, selected custom paths and recovery state. |
| `upgrade.log` | Migration output, once migration starts. |

The tar bundle contains the database dump, configuration (including the root
files above), custom components,
optional data and recovery metadata as it stood **before migration**. The SSH
archive contains the same components. Archives do not include the full old
application code or logs written after the backup was taken. Keep the matching
Nextcloud release available for recovery.

Files or directories ending in `.part` or `.partial` are incomplete backups and
must not be used as verified restore sources.

For a local bundle, verify its checksum from its backup directory, then inspect
or extract it into a separate, empty working directory:

```bash
cd /path/to/upgrade-TIMESTAMP.RANDOM
sha256sum -c backup.tar.sha256
tar -tf backup.tar
sudo tar --acls --xattrs --numeric-owner -xpf backup.tar -C /path/to/empty/restore-directory
```

For an SSH archive, use its downloaded `.tar` path with the same inspection and
extraction commands; a checksum sidecar is created only for local bundles.
The resulting `config/`, `database.sql.gz`, `custom/` (when present) and optional
`data/` can be used as a matching set following
[Nextcloud's restore guidance](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/restore.html).
Copy any entries in `custom/` to their corresponding installation-relative
locations when restoring the old release, and restore `.htaccess`/`.user.ini`
when present. Nextcloud may regenerate `.htaccess` during an upgrade; review
custom directives afterward.
Restore while access and background jobs are blocked, using application code
matching `previous_version` in `manifest`, and restore the SQL dump into a clean
database as described in the linked guide. Check file ownership and permissions
against the recorded HTTP account/group before starting services. If `data/` is absent, recover data
through your existing backup system as needed; a SQL dump and configuration
alone cannot recover lost user files. For an outdated data backup, consult the
restore guide's client-synchronization advice before reconnecting clients.
This script does not automatically restore data or import a database after
migration has started.

The previous application is retained alongside the installation in a unique
`.old.upgrade-…` directory. Successful upgrades remove disposable download and
staging files, but keep the recovery backups and old application. Remove obsolete
recovery copies deliberately after verifying the upgrade; check their exact paths
and ensure they contain no user-data mounts before deleting them.

If an upgrade fails:

- **Before database migration:** the script attempts to restore the original
  application, services, cron and maintenance state. Confirm that recovery
  succeeded before retrying. A changed or unavailable data directory leaves
  access blocked for manual investigation.
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

`occ` runs Nextcloud commands as `WEB_USER` with `PHP_BIN`; installed build
metadata is also loaded as that user. Other `php_info` modes parse structured
metadata and validate downloaded releases/status. `stop_services`, `start_services` and
`wait_service` handle service control through `service_control`; `check_services`
validates the selected backend's prerequisites. `restore_cron` checks for
concurrent edits.
`add_preserved_path` builds the transfer list and rejects collisions;
`check_preserved_path` validates entries before copying. `check_application_tree`
rejects unsupported source files and mounts before preparation and after shutdown;
`check_file_links` verifies internal file targets. Archive link checks also prevent
links from redirecting extraction.
`find_application` prunes internal data from application scans; live data is moved
only after staged ownership/permission changes. `restore_internal_data` moves it
back before a pre-migration application rollback. `backup_data` verifies a stopped
data copy; `backup_stream` supplies the same restore components to local packaging
and optional SSH transfer.
Keep ownership/deletion operations away from the user-data directory.

Run the same checks used by GitHub Actions from the repository:

```bash
bash .github/scripts/check.sh
```

Checks require ShellCheck, Python 3, PHP 8.4 or newer with SimpleXML, both archive
toolsets and `rsync` for data backup cases. Set `TEST_PHP=/path/to/php` to select
another PHP executable. Missing dependencies or skipped tests fail the shared
check command. Tests use isolated
files and mocked Nextcloud, database and service commands; no root access,
network connection, Docker or running Nextcloud installation is required. The
release tests build, verify, unpack and install the package into a temporary
directory. Preserve
coverage of failures around backup, replacement, migration and restoration.

### Publishing a script release

The script starts at `0.1.0`; its version is independent of Nextcloud's. During
initial development, increment the patch for fixes and the minor version for
new features or changed behavior. Reserve `1.0.0` for a stable documented
interface, following [Semantic Versioning](https://semver.org/).

Update `SCRIPT_VERSION` near the top of the script, run the checks, and merge
the changes into `main`. Tag that commit with the matching version and push the
tag, for example from an up-to-date `main` checkout:

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

Pushing the version tag starts **Prepare release** in GitHub Actions. It tests
the tagged commit, then creates a **draft release** containing
`nextcloud-upgrade-v0.1.0.tar.gz`, its `.sha256` file and generated release notes.
Tags must match `SCRIPT_VERSION`; a tag such as `v0.1.0-rc.1` is also marked as
a pre-release. Use the built-in `GITHUB_TOKEN`; no personal access token or
custom repository secret is needed.

Once the workflow succeeds, open the draft under **Releases**, review the notes
and both downloads, then click **Publish release**. Publication is manual;
the workflow never publishes or replaces an existing release. If preparation
fails and leaves an incomplete draft, delete that draft before rerunning the
failed job.
Release immutability can stay enabled: all files are attached before
publication, following [GitHub's recommended sequence](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).

To build the same package locally without publishing it:

```bash
bash .github/scripts/package-release.sh v0.1.0
```

This writes the archive and checksum into `dist/`. The package includes only
`nextcloud-upgrade.sh`, `README.md` and `LICENSE.md`. CI runs the same checks for
PRs targeting `main` and every push to `main`, including merges. To require a
passing result before merging, select the `Checks` status in the repository's
branch protection or ruleset; workflow files alone do not enable that policy.

For supported server/PHP combinations and release-specific upgrade requirements,
consult Nextcloud's [system requirements](https://docs.nextcloud.com/server/stable/admin_manual/installation/system_requirements.html),
[manual upgrade instructions](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/manual_upgrade.html)
and [release notes](https://docs.nextcloud.com/server/stable/admin_manual/release_notes/index.html).
Use documentation matching the installed and target releases; the `/latest/`
manual can describe an upcoming development version. The script validates CLI
PHP against the downloaded release; web-PHP extensions and database-version
compatibility must also meet that release's requirements.

## License

[MIT](LICENSE.md). This project is not affiliated with or endorsed by Nextcloud.
