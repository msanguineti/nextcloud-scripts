#!/bin/bash
# Upgrade one conventional Nextcloud installation. See README.md for usage and recovery.
set -Eeuo pipefail
umask 077
# Version of this tool, independent of the Nextcloud version being installed.
readonly SCRIPT_VERSION='1.0.0'

usage() {
    printf 'Nextcloud Upgrade Script %s\n\n' "$SCRIPT_VERSION"
    printf '%s\n' 'Usage: nextcloud-upgrade.sh [stable-version]
       nextcloud-upgrade.sh --help
       nextcloud-upgrade.sh --version

Upgrade one archive-based Nextcloud installation on a systemd Linux host.
Without a version, discover the next eligible stable release. An explicit
version must be within the currently installed major release. Confirmation
is required before maintenance mode, service changes or backups begin.

Options are environment variables (pass them with sudo env):
  NEXTCLOUD_PATH          Installation directory (/var/www/nextcloud)
  BACKUP_PATH             Existing recovery directory (/var/backups/nextcloud)
  WEB_USER                Nextcloud/cron account (www-data)
  WEB_GROUP               Staged application group (primary group of WEB_USER)
  PHP_BIN                 PHP CLI executable, preferably absolute (php)
  SERVICE_MANAGER         monit or systemd (monit)
  WEB_SERVICE             Web-server service (nginx)
  PHP_SERVICE             Separate PHP service, or none (php8.4-fpm)
  BACKUP_DATA             Copy and verify filesystem data: yes/no (no)
  BACKUP_ARCHIVE          Also create backup.tar and checksum: yes/no (no)
  BACKUP_SSH_DEST         Optional extra archive: user@host:/absolute/directory
  PRESERVE_PATHS_FILE     Optional list of extra installation-relative paths
  DOWNLOAD_FORMAT        tar.bz2 or zip (tar.bz2)
  CHECKSUM_TYPE           sha256, sha512 or md5 (sha256)
  WAIT_AFTER_MAINTENANCE  Grace period before stopping web/PHP services (30)
  WAIT_BEFORE_BACKUP      Maximum wait for existing cron/occ jobs to finish (60)
  WAIT_AFTER_SERVER_START Seconds to wait for each service state change (20)
  NO_COLOR               Set to disable terminal colours

Examples:
  sudo nextcloud-upgrade.sh
  sudo env SERVICE_MANAGER=systemd BACKUP_ARCHIVE=yes nextcloud-upgrade.sh
  sudo env WEB_USER=apache WEB_GROUP=apache WEB_SERVICE=httpd \
      PHP_SERVICE=php-fpm PHP_BIN=/usr/bin/php nextcloud-upgrade.sh 34.0.1

See README.md for requirements, supported layouts, backups and recovery.'
}
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    [[ $# == 1 ]] || { printf '%s\n' 'Use --help on its own.' >&2; exit 2; }
    usage
    exit 0
fi
if [[ ${1:-} == --version ]]; then
    [[ $# == 1 ]] || { printf '%s\n' 'Use --version on its own.' >&2; exit 2; }
    printf 'nextcloud-upgrade %s\n' "$SCRIPT_VERSION"
    exit 0
fi

NEXTCLOUD_PATH="${NEXTCLOUD_PATH:-/var/www/nextcloud}"
BACKUP_PATH="${BACKUP_PATH:-/var/backups/nextcloud}"
WEB_USER="${WEB_USER:-www-data}"
WEB_GROUP="${WEB_GROUP:-}"
PHP_BIN="${PHP_BIN:-php}"
SERVICE_MANAGER="${SERVICE_MANAGER:-monit}"
WEB_SERVICE="${WEB_SERVICE:-nginx}"
# Use 'none' only when PHP runs inside the web-server process (e.g. mod_php).
PHP_SERVICE="${PHP_SERVICE:-php8.4-fpm}"
managed_services=("$WEB_SERVICE")
if [[ $PHP_SERVICE != none ]]; then
    managed_services+=("$PHP_SERVICE")
fi
WAIT_AFTER_MAINTENANCE="${WAIT_AFTER_MAINTENANCE:-30}"
WAIT_BEFORE_BACKUP="${WAIT_BEFORE_BACKUP:-60}"
WAIT_AFTER_SERVER_START="${WAIT_AFTER_SERVER_START:-20}"
DOWNLOAD_FORMAT="${DOWNLOAD_FORMAT:-tar.bz2}"
CHECKSUM_TYPE="${CHECKSUM_TYPE:-sha256}"
# Plain text, one installation-relative path per line; never sourced as shell code.
PRESERVE_PATHS_FILE="${PRESERVE_PATHS_FILE:-}"
BACKUP_DATA="${BACKUP_DATA:-no}"
BACKUP_ARCHIVE="${BACKUP_ARCHIVE:-no}"
BACKUP_SSH_DEST="${BACKUP_SSH_DEST:-}"
preserved_paths=()
data_relative=''

phase=preflight
workspace='' stage='' recovery='' old_path='' credentials=''
production_changed=no
replacement_started=no
migration_started=no
cron_changed=no
cron_was_present=not-inspected
manage_cron=no
background_mode=''
finished=no
upgrade_started=$SECONDS
offline_started=''
progress_pid=''

log() {
    local symbol='[i]' color=36
    case ${1:-} in
        --info) shift ;;
        --success) symbol='[✓]'; color=32; shift ;;
        --warning) symbol='[!]'; color=33; shift ;;
        --error) symbol='[x]'; color=31; shift ;;
        --question) symbol='[?]'; color=34; shift ;;
    esac
    if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR+x} ]]; then
        printf '\033[1;%sm%s\033[0m [%s] %s\n' "$color" "$symbol" "$phase" "$*"
    else
        printf '%s [%s] %s\n' "$symbol" "$phase" "$*"
    fi
}
# Status goes to stderr so command output can still be saved or piped safely.
stop_progress() {
    if [[ -n $progress_pid ]]; then
        kill "$progress_pid" 2>/dev/null || true
        wait "$progress_pid" 2>/dev/null || true
        progress_pid=''
    fi
}
start_progress() {
    stop_progress
    local message=$1 started=$SECONDS
    log "$message" >&2
    (
        # This display process must never inherit the upgrade's recovery traps.
        trap - EXIT ERR INT TERM HUP
        set +e
        interactive=no spinner_line='' frames="|/-\\" frame=0 timer=''
        [[ -t 2 && ${TERM:-dumb} != dumb ]] && interactive=yes
        trap '
            for timer in $(jobs -pr); do kill "$timer" 2>/dev/null; done
            wait
            [[ -z $spinner_line ]] || printf "\r%*s\r" "${#spinner_line}" "" >&2
        ' EXIT
        trap 'exit 0' INT TERM HUP
        while :; do
            if [[ $interactive == yes ]]; then
                # This is activity feedback, not a measured completion percentage.
                printf -v spinner_line '  [%s] %ss elapsed' "${frames:frame:1}" "$((SECONDS - started))"
                if [[ -z ${NO_COLOR+x} ]]; then
                    printf '\r\033[1;36m%s\033[0m' "$spinner_line" >&2
                else
                    printf '\r%s' "$spinner_line" >&2
                fi
                frame=$(((frame + 1) % 4))
                sleep 0.1 &
            else
                sleep 15 &
            fi
            wait "$!" || exit 0
            if [[ $interactive == no ]]; then
                log "$message ($((SECONDS - started))s elapsed; still running)." >&2
            fi
        done
    ) &
    progress_pid=$!
}
with_progress() {
    local message=$1
    shift
    start_progress "$message"
    # Keep the actual operation in the foreground, with the normal error traps.
    "$@"
    stop_progress
}

fail() { log --error "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required command missing: $1"; }
occ() { (cd "$NEXTCLOUD_PATH" && sudo -u "$WEB_USER" "$PHP_BIN" ./occ "$@"); }
config() { occ config:system:get "$1"; }

fetch_config_originals() {
    local path reference fetched
    # config/ is excluded from Nextcloud's signed hashes; read only its shipped templates.
    for path in config/.htaccess config/config.sample.php; do
        [[ -f $NEXTCLOUD_PATH/$path && ! -L $NEXTCLOUD_PATH/$path ]] || continue
        if [[ -f $stage/nextcloud/$path ]] && cmp -s -- "$NEXTCLOUD_PATH/$path" "$stage/nextcloud/$path"; then continue; fi
        reference="$workspace/shipped-config/$path"
        mkdir -p -- "$(dirname -- "$reference")"
        start_progress "Reading original $path from Nextcloud $current_release"
        if curl --fail --silent --location --proto '=https' --proto-redir '=https' \
            --connect-timeout 10 --max-time 20 -o "$reference.part" \
            "https://raw.githubusercontent.com/nextcloud/server/v$current_release/$path"; then
            fetched=yes
        else
            fetched=no
        fi
        stop_progress
        if [[ $fetched == yes ]]; then
            mv -T -- "$reference.part" "$reference"
        else
            rm -f -- "$reference.part"
            log --warning "Cannot fetch the original $path; retaining it if its origin cannot be determined."
        fi
    done
}

prepare_local_files() {
    local action=$1 kind path destination ancestor
    php_info local-files "$NEXTCLOUD_PATH" "$stage/nextcloud" "$workspace/shipped-config" > "$workspace/local-files"
    while IFS= read -r -d '' kind; do
        IFS= read -r -d '' path || fail 'Incomplete local-file classification.'
        case $kind in
            baseline)
                log --warning 'Installed release hashes are unavailable; retaining files whose origin cannot be determined.'
                continue ;;
            stock)
                [[ $action == apply ]] || log "Using new release defaults instead of unmodified file: $path"
                continue ;;
            custom)
                log --warning "Preserving modified shipped file: $path. Review it against the new release; integrity warnings may remain." ;;
            unknown)
                log --warning "No usable original hash for $path; retaining the existing file. Review it against the new release." ;;
            local|directory) ;;
            *) fail "Unknown local-file classification: $kind" ;;
        esac
        destination="$stage/nextcloud/$path"
        ancestor=$(dirname -- "$destination")
        while [[ $ancestor != "$stage/nextcloud" ]]; do
            [[ ! -L $ancestor && ( ! -e $ancestor || -d $ancestor ) ]] || fail "Local file conflicts with a release path: $path"
            ancestor=$(dirname -- "$ancestor")
        done
        if [[ $kind == directory ]]; then
            [[ ! -L $destination && ( ! -e $destination || -d $destination ) ]] || fail "Local directory conflicts with a release file: $path"
            if [[ $action == apply ]]; then mkdir -p -- "$destination"; fi
        else
            [[ ! -L $destination && ! -d $destination ]] || fail "Local file conflicts with a release directory or link: $path"
            if [[ $action == apply ]]; then
                mkdir -p -- "$(dirname -- "$destination")"
                cp -aT -- "$NEXTCLOUD_PATH/$path" "$destination"
            fi
        fi
    done < "$workspace/local-files"
}

find_application() {
    if [[ -n $data_relative ]]; then
        find "$NEXTCLOUD_PATH" -samefile "$data_path" -prune -o "$@"
    else
        find "$NEXTCLOUD_PATH" "$@"
    fi
}

check_file_links() {
    local tree=$1 list=$2 link target resolved
    while IFS= read -r -d '' link; do
        target=$(readlink -- "$link")
        resolved=$(realpath -e -- "$link") || fail "Unsupported application link (missing target): $link"
        [[ $target != /* && $target != *[[:cntrl:]]* && -f $resolved && $resolved == "$tree/"* ]] || fail "Unsupported application link (must resolve to an internal file): $link"
        if [[ $tree == "$NEXTCLOUD_PATH" ]]; then
            [[ $resolved != "$data_path" && $resolved != "$data_path/"* ]] || fail "Unsupported application link into user data: $link"
        fi
        if [[ $link == "$tree/config/"* ]]; then
            [[ $resolved == "$tree/config/"* ]] || fail "Configuration links must stay inside config/: $link"
        fi
    done < "$list"
}

check_application_tree() {
    local mounts mount unsupported
    mounts=$(findmnt --raw --noheadings --output TARGET)
    while IFS= read -r mount; do
        printf -v mount '%b' "$mount" # findmnt --raw escapes spaces and backslashes.
        [[ $mount != "$NEXTCLOUD_PATH" && $mount != "$NEXTCLOUD_PATH/"* ]] || fail "Mount inside application tree: $mount"
    done <<< "$mounts"
    unsupported=$(find_application ! -type d ! -type f ! -type l -print -quit)
    [[ -z $unsupported ]] || fail "Only regular files and directories, plus internal file symlinks, are supported in the application tree: $unsupported"
    find_application -type l -print0 > "$workspace/application.links"
    check_file_links "$NEXTCLOUD_PATH" "$workspace/application.links"
}

check_preserved_path() {
    local path=$1 ancestor=$1
    [[ -n $path && $path != /* && $path != */ && $path != *[[:cntrl:]]* ]] || fail "Invalid preservation path: $path"
    case /$path/ in *'/../'*|*'/./'*|*'//'*) fail "Use a canonical installation-relative preservation path: $path";; esac
    case $path in
        config|config/*|data|data/*|.htaccess|.htaccess/*|.user.ini|.user.ini/*)
            fail "Preservation path is reserved or already handled automatically: $path" ;;
    esac
    if [[ -n $data_relative ]]; then
        [[ $path != "$data_relative" && $path != "$data_relative/"* && $data_relative != "$path/"* ]] || fail "User data is handled separately, not as a custom preservation path: $path"
    fi
    [[ -f $NEXTCLOUD_PATH/$path || -d $NEXTCLOUD_PATH/$path ]] || fail "Missing preservation file or directory: $path"
    [[ $(realpath -e -- "$NEXTCLOUD_PATH/$path") == "$NEXTCLOUD_PATH/$path" ]] || fail "Preservation path must not traverse symlinks: $path"
    [[ ! -e $stage/nextcloud/$path && ! -L $stage/nextcloud/$path ]] || fail "Preservation path would overwrite a release file or directory: $path"
    while [[ $ancestor == */* ]]; do
        ancestor=${ancestor%/*}
        [[ ! -e $stage/nextcloud/$ancestor || -d $stage/nextcloud/$ancestor ]] || fail "Preservation path has a file as its destination parent: $path"
    done
}

add_preserved_path() {
    local path=$1 existing
    check_preserved_path "$path"
    for existing in "${preserved_paths[@]}"; do
        [[ $path != "$existing" && $path != "$existing/"* && $existing != "$path/"* ]] || fail "Overlapping preservation paths: $path and $existing"
    done
    preserved_paths+=("$path")
}

check_custom_apps() {
    local apps_dir app
    while IFS= read -r apps_dir; do
        [[ -d $NEXTCLOUD_PATH/$apps_dir ]] || fail "Missing apps directory: $apps_dir"
        for app in "$NEXTCLOUD_PATH/$apps_dir/"*; do
            [[ -d $app ]] || continue
            [[ ! -e $stage/nextcloud/apps/${app##*/} ]] || fail "Custom app conflicts with an app shipped by the release: $apps_dir/${app##*/} (apps/${app##*/})"
        done
    done < "$workspace/extra-app-paths"
}

backup_data() {
    log "Copying user data from $data_path; services remain stopped."
    mkdir -- "$recovery/data.partial"
    rsync -aHAX --numeric-ids -- "$data_path/" "$recovery/data.partial/"
    # Read both copies to verify content and metadata, without modifying either.
    rsync -aHAXnc --numeric-ids --delete --itemize-changes -- "$data_path/" "$recovery/data.partial/" > "$recovery/data.verify.log"
    [[ ! -s $recovery/data.verify.log ]] || fail 'Data backup verification found differences; keep data.partial for inspection.'
    sync -f "$recovery/data.partial"
    mv -T -- "$recovery/data.partial" "$recovery/data"
    log --success 'Data backup copied and verified.'
}

shell_quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
backup_stream() {
    local file entries=(config database.sql.gz manifest preserved-paths phase)
    if [[ $manage_cron == yes ]]; then entries+=(cron.original cron.disabled); fi
    for file in .htaccess .user.ini; do
        if [[ -f $recovery/$file ]]; then entries+=("$file"); fi
    done
    if [[ -d $recovery/custom ]]; then entries+=(custom); fi
    if [[ $BACKUP_DATA == yes ]]; then entries+=(data data.verify.log); fi
    tar --acls --xattrs --numeric-owner -cf - -C "$recovery" -- "${entries[@]}"
}
package_backups() {
    log 'Packaging recovery backups into backup.tar.'
    backup_stream > "$recovery/backup.tar.partial"
    tar -tf "$recovery/backup.tar.partial" > /dev/null
    mv -T -- "$recovery/backup.tar.partial" "$recovery/backup.tar"
    (cd "$recovery" && sha256sum backup.tar > backup.tar.sha256)
    sync -f "$recovery"
    log --success 'Restore bundle created with its SHA256 checksum.'
}
backup_ssh() {
    ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -- "$ssh_host" "$1"
}
copy_backups_offsite() {
    local remote_file remote_partial
    remote_file=$(shell_quote "$ssh_directory/${recovery##*/}.tar")
    remote_partial=$(shell_quote "$ssh_directory/${recovery##*/}.tar.partial")
    log "Sending recovery backup archive to $ssh_host."
    # SSH authenticates the transfer; archive metadata survives non-root storage.
    # Noclobber and unique attempt names preserve previous remote backups.
    local command="set -e; umask 077; set -C; test ! -e $remote_file; test ! -L $remote_file; cat > $remote_partial; tar -tf $remote_partial > /dev/null; mv -T -- $remote_partial $remote_file; sync -f $remote_file"
    if [[ $BACKUP_ARCHIVE == yes ]]; then
        backup_ssh "$command" < "$recovery/backup.tar"
    else
        backup_stream | backup_ssh "$command"
    fi
    log --success 'Recovery backup archive copied off-server.'
}

# The data directory follows the code replacement by rename, never by chmod/copy.
restore_internal_data() {
    [[ -n $data_relative ]] || return 0
    if [[ -d $old_path/$data_relative && ! -e $NEXTCLOUD_PATH/$data_relative && ! -L $NEXTCLOUD_PATH/$data_relative ]]; then
        return 0
    fi
    [[ ! -e $old_path/$data_relative && ! -L $old_path/$data_relative && -d $NEXTCLOUD_PATH/$data_relative && ! -L $NEXTCLOUD_PATH/$data_relative ]] || return 1
    mv -T -- "$NEXTCLOUD_PATH/$data_relative" "$old_path/$data_relative" || return 1
}

# PHP is already required by Nextcloud; use it for structured data, not regex XML.
php_info() {
    local runner=("$PHP_BIN")
    if [[ $1 == build ]]; then runner=(sudo -u "$WEB_USER" "$PHP_BIN"); fi
    "${runner[@]}" -r "$(cat <<'PHP'
function bad($message) { fwrite(STDERR, $message . "\n"); exit(1); }
$mode = $argv[1];
if ($mode === 'build') {
    // This mode runs as WEB_USER: installed PHP must never execute as root.
    require $argv[2] . '/version.php';
    echo urlencode($OC_Build) . 'x' . PHP_MAJOR_VERSION . 'x' . PHP_MINOR_VERSION . 'x' . PHP_RELEASE_VERSION;
} elseif ($mode === 'update') {
    $body = trim(stream_get_contents(STDIN));
    if ($body === '') exit(0);
    libxml_use_internal_errors(true);
    if (stripos($body, '<!DOCTYPE') !== false) bad('DOCTYPE is not allowed in update response');
    $xml = simplexml_load_string($body, 'SimpleXMLElement', LIBXML_NONET);
    if ($xml === false || $xml->getName() !== 'nextcloud') bad('Malformed update-server response');
    if (count($xml->children()) === 0) exit(0);
    if (count($xml->version) !== 1 || !preg_match('/^\d+\.\d+\.\d+(\.\d+)?$/D', (string)$xml->version)) bad('Invalid stable release version');
    if (isset($xml->autoupdater) && (string)$xml->autoupdater !== '1') bad('Update server requires a manual update');
    echo (string)$xml->version;
} elseif ($mode === 'status') {
    $s = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    if (($s['installed'] ?? false) !== true || ($s['needsDbUpgrade'] ?? true) !== false) bad('Installation is absent or needs database migration');
    if ($argv[2] !== 'any' && ($s['maintenance'] ?? null) !== ($argv[2] === 'on')) bad('Unexpected maintenance state');
    if (isset($argv[3]) && implode('.', array_slice(explode('.', $s['version']), 0, 3)) !== $argv[3]) bad('Installed version differs from target');
    echo $s['version'];
} elseif ($mode === 'release') {
    require $argv[2] . '/version.php';
    $v = implode('.', array_slice($OC_Version, 0, 3));
    if ($v !== $argv[3] || $OC_Channel !== 'stable') bad('Extracted release does not match the stable target');
    // This performs Nextcloud's own PHP compatibility check without loading the app.
    require $argv[2] . '/lib/versioncheck.php';
} elseif ($mode === 'local-files') {
    $root = $argv[2];
    $release = $argv[3];
    $signature = @file_get_contents($root . '/core/signature.json');
    $hashes = $signature === false ? null : (json_decode($signature, true)['hashes'] ?? null);
    $record = static function ($kind, $path) { echo $kind, "\0", $path, "\0"; };
    if (!is_array($hashes)) {
        $hashes = [];
        $record('baseline', 'core/signature.json');
    }
    $paths = ['.htaccess', '.user.ini', 'config'];
    $files = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($root . '/config', FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::SELF_FIRST);
    foreach ($files as $file) $paths[] = substr($file->getPathname(), strlen($root) + 1);
    foreach ($paths as $path) {
        $file = $root . '/' . $path;
        if (!file_exists($file)) continue;
        if (is_dir($file)) { $record('directory', $path); continue; }
        // Active configuration belongs to the installation, even if a manifest lists it.
        if ($path === 'config/config.php' || str_ends_with($path, '.config.php')) {
            $record('local', $path);
            continue;
        }
        // Changing a regular shipped file into a link is itself a customization.
        if (is_link($file)) {
            $record(isset($hashes[$path]) || in_array($path, ['.htaccess', '.user.ini'], true) ? 'custom' : 'local', $path);
            continue;
        }
        $rawHash = hash_file('sha512', $file);
        if ($rawHash === false) bad('Cannot inspect local file: ' . $path);
        $hash = $rawHash;
        // Match Nextcloud's checksum rules: its generated .htaccess tail is excluded.
        if ($path === '.htaccess') {
            $content = file_get_contents($file);
            if ($content === false) bad('Cannot inspect local file: ' . $path);
            $parts = explode('#### DO NOT CHANGE ANYTHING ABOVE THIS LINE ####', $content);
            if (count($parts) === 2) $hash = hash('sha512', $parts[0]);
        }
        $original = $hashes[$path] ?? null;
        if (!is_string($original) || !preg_match('/^[a-f0-9]{128}$/iD', $original)) {
            $reference = $argv[4] . '/' . $path;
            $original = is_file($reference) ? hash_file('sha512', $reference) : null;
        }
        if (is_string($original) && preg_match('/^[a-f0-9]{128}$/iD', $original)) {
            $kind = hash_equals(strtolower($original), $hash) ? 'stock' : 'custom';
        } elseif (is_file($release . '/' . $path)) {
            // Identical new defaults need no old baseline; otherwise retain unknown files.
            $kind = $rawHash === hash_file('sha512', $release . '/' . $path) ? 'stock' : 'unknown';
        } else {
            $kind = in_array($path, ['.htaccess', '.user.ini'], true) ? 'unknown' : 'local';
        }
        $record($kind, $path);
    }
} elseif ($mode === 'custom-app') {
    libxml_use_internal_errors(true);
    $xml = simplexml_load_file($argv[2] . '/appinfo/info.xml', 'SimpleXMLElement', LIBXML_NONET);
    if ($xml === false) bad('Cannot classify old app; inspect its appinfo/info.xml');
    echo strtolower((string)($xml->shipped ?? 'false')) === 'true' ? 'no' : 'yes';
} elseif ($mode === 'apps') {
    $s = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR)['system'];
    foreach ($s['apps_paths'] ?? [] as $a) {
        $p = $a['path'];
        if ($p === $argv[2] . '/apps') continue;
        if (!str_starts_with($p, $argv[2] . '/') || !preg_match('~^[A-Za-z0-9_-]+$~D', substr($p, strlen($argv[2]) + 1))) bad('Unsupported external or nested apps path; preserve it explicitly before upgrading');
        echo substr($p, strlen($argv[2]) + 1) . "\n";
    }
} elseif ($mode === 'local-data') {
    $s = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR)['system'];
    if (!empty($s['objectstore']) || !empty($s['objectstore_multibucket'])) bad('BACKUP_DATA only backs up a filesystem data directory, not primary object storage');
}
PHP
)" "$@"
}

set_phase() {
    phase=$1
    if [[ -n $recovery ]]; then
        printf '%s\n' "$phase" > "$recovery/phase.new"
        mv -T -- "$recovery/phase.new" "$recovery/phase"
        # Persist recovery boundaries before filesystem changes or migration.
        sync -f "$recovery"
    fi
    log "${3:---info}" "$2"
}

# Keep backend selection out of the upgrade and recovery lifecycle.
service_control() {
    local action=$1 service=$2
    case $SERVICE_MANAGER in
        monit) monit "$action" "$service" ;;
        systemd) systemctl "$action" "$service" ;;
        *) return 1 ;;
    esac
}

check_services() {
    local service
    [[ ${WEB_SERVICE%.service} != "${PHP_SERVICE%.service}" ]] || fail 'WEB_SERVICE and PHP_SERVICE must be different; use PHP_SERVICE=none for embedded PHP.'
    if [[ $PHP_SERVICE == none && ${WEB_SERVICE%.service} == nginx ]]; then
        fail 'Nginx requires a separate PHP-FPM service; PHP_SERVICE=none is for embedded PHP.'
    fi
    for service in "${managed_services[@]}"; do
        [[ $service =~ ^[A-Za-z0-9_.@-]+$ ]] || fail 'Invalid service name.'
        [[ $(systemctl show "$service" --property=LoadState --value) == loaded ]] || fail "Unknown systemd service: $service"
        [[ $(systemctl show "$service" --property=ActiveState --value) == active ]] || fail "Service must already be running: $service"
        if [[ $SERVICE_MANAGER == monit ]]; then
            monit -B status "$service" | grep -Eiq 'monitoring status[[:space:]]+Monitored' || fail "Service must already be monitored: $service"
        fi
    done
}

wait_service() {
    local service=$1 expected=$2 state i
    for ((i=0; i<=WAIT_AFTER_SERVER_START; i++)); do
        state=$(systemctl show "$service" --property=ActiveState --value) || return 1
        [[ $state == "$expected" ]] && return 0
        ((i == WAIT_AFTER_SERVER_START)) || sleep 1
    done
    log --error "Service $service did not reach $expected (last state: $state)" >&2
    return 1
}
stop_services() {
    local rc=0 service
    # Stop the frontend first; still try every service when one stop fails.
    for service in "${managed_services[@]}"; do
        service_control stop "$service" && wait_service "$service" inactive || rc=1
    done
    return "$rc"
}
start_services() {
    local i service
    # Start a separate PHP runtime before the frontend, when configured.
    for ((i=${#managed_services[@]}-1; i>=0; i--)); do
        service=${managed_services[i]}
        service_control start "$service" || return 1
        wait_service "$service" active || return 1
    done
}
read_cron() {
    local destination=$1 rc
    if LC_ALL=C crontab -u "$WEB_USER" -l > "$destination" 2> "$destination.error"; then
        cron_read_present=yes
        return 0
    else
        rc=$?
    fi
    if [[ $rc == 1 ]] && grep -Fxq "no crontab for $WEB_USER" "$destination.error"; then
        cron_read_present=no
        : > "$destination"
        return 0
    fi
    cat "$destination.error" >&2
    return 1
}
restore_cron() {
    [[ $manage_cron == yes ]] || return 0
    [[ $cron_changed == yes || $cron_was_present == no ]] || return 0
    read_cron "$recovery/cron.current" || return 1
    if cmp -s "$recovery/cron.original" "$recovery/cron.current"; then
        cron_changed=no
        return 0
    fi
    if [[ $cron_was_present != yes ]] || ! cmp -s "$recovery/cron.disabled" "$recovery/cron.current"; then
        log --warning 'Crontab changed externally; reconcile cron.original and cron.current manually.' >&2
        return 1
    fi
    crontab -u "$WEB_USER" "$recovery/cron.original" || return 1
    cron_changed=no
}

on_exit() {
    local rc=$? safe=yes
    trap - EXIT ERR INT TERM HUP
    set +e
    stop_progress
    [[ -z $credentials ]] || rm -f -- "$credentials"
    if [[ $rc != 0 ]]; then
        log --error "FAILED (exit $rc). No successful upgrade is being reported." >&2
        if [[ $production_changed == yes ]]; then
            # Re-establish the marker even if the final marker removal/sync failed.
            printf '%s\n' "$recovery" > "$BACKUP_PATH/upgrade-active"
            sync -f "$BACKUP_PATH"
            printf 'exit=%s\nphase=%s\nmigration_started=%s\n' "$rc" "$phase" "$migration_started" > "$recovery/failure"

            if [[ $migration_started == yes ]]; then
                # Re-disable only an account crontab that this run manages.
                if [[ $manage_cron == yes ]]; then
                    if read_cron "$recovery/cron.current"; then
                        if cmp -s "$recovery/cron.original" "$recovery/cron.current"; then
                            if [[ $cron_was_present == yes || $cron_read_present == yes ]]; then
                                crontab -u "$WEB_USER" "$recovery/cron.disabled" || log --error 'Disable Nextcloud cron manually now.' >&2
                            fi
                        elif ! cmp -s "$recovery/cron.disabled" "$recovery/cron.current"; then
                            log --warning 'Crontab changed externally; disable Nextcloud cron manually now.' >&2
                        fi
                    else
                        log --error 'Cannot inspect crontab; disable Nextcloud cron manually now.' >&2
                    fi
                fi
                # Code alone cannot undo a database migration. Keep every artifact.
                occ maintenance:mode --on >/dev/null 2>&1
                stop_services || log --error 'Could not confirm services stopped; block access manually now.' >&2
                log --warning 'Database migration may have changed state. Keep maintenance enabled and services stopped; see README.md (Backups and troubleshooting) for manual recovery.' >&2
                safe=no
            else
                stop_services || safe=no
                if [[ $safe == yes && $replacement_started == yes && -d $old_path ]]; then
                    restore_internal_data || safe=no
                    if [[ $safe == yes && -e $NEXTCLOUD_PATH ]]; then
                        mv -T -- "$NEXTCLOUD_PATH" "$stage/failed-release" || safe=no
                    fi
                    if [[ $safe == yes ]]; then
                        mv -T -- "$old_path" "$NEXTCLOUD_PATH" || safe=no
                    fi
                fi
                if [[ $safe == yes ]]; then
                    [[ -d $data_path && $(stat -c '%d:%i' "$data_path") == "$data_identity" ]] || safe=no
                    [[ $(realpath -e -- "$(config datadirectory)") == "$data_path" ]] || safe=no
                fi
                if [[ $safe == yes ]]; then
                    occ status --output=json | php_info status any "$current_release" >/dev/null || safe=no
                fi
                if [[ $safe == yes ]]; then
                    start_services || safe=no
                fi
                if [[ $safe == yes ]]; then
                    restore_cron || safe=no
                fi
                if [[ $safe == yes ]]; then
                    occ maintenance:mode --off || safe=no
                fi
                if [[ $safe == yes ]]; then
                    printf 'recovered-before-migration\n' > "$recovery/phase"
                    sync -f "$recovery"
                    rm -f -- "$BACKUP_PATH/upgrade-active"
                    log --success 'Original application restored; no database migration was started.' >&2
                else
                    occ maintenance:mode --on >/dev/null 2>&1
                    stop_services || log --error 'Block access manually; service shutdown could not be verified.' >&2
                    log --error 'Automatic recovery incomplete. Keep access blocked and see README.md (Backups and troubleshooting).' >&2
                fi
            fi
        fi
        [[ -z $recovery ]] || log "Recovery artifacts: $recovery; phase: $phase; old application: $old_path; staging: $stage; workspace: $workspace" >&2
        [[ -n $recovery ]] || { [[ -z $stage ]] || rm -rf --one-file-system -- "$stage"; [[ -z $workspace ]] || rm -rf --one-file-system -- "$workspace"; }
    elif [[ $finished == yes || $production_changed == no ]]; then
        [[ -z $stage ]] || rm -rf --one-file-system -- "$stage"
        [[ -z $workspace ]] || rm -rf --one-file-system -- "$workspace"
    fi
    exit "$rc"
}
trap on_exit EXIT
# Never log BASH_COMMAND: it could contain credentials.
trap 'rc=$?; log --error "Operation failed at script line $LINENO (exit $rc)." >&2; exit "$rc"' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

[[ $EUID == 0 ]] || fail 'Run as root.'
[[ $# -le 1 ]] || fail 'Usage: nextcloud-upgrade.sh [stable-version]'
case $BACKUP_DATA in yes) need rsync;; no) ;; *) fail 'BACKUP_DATA must be yes or no.';; esac
case $BACKUP_ARCHIVE in yes) need tar; need sha256sum;; no) ;; *) fail 'BACKUP_ARCHIVE must be yes or no.';; esac
case $SERVICE_MANAGER in
    monit) need monit ;;
    systemd) ;;
    *) fail 'SERVICE_MANAGER must be monit or systemd.' ;;
esac
for cmd in cat sudo curl mktemp flock realpath readlink stat find findmnt df du awk grep sort head tail wc dirname date cmp cp mv rm chmod chown mkdir gzip tee sync sleep systemctl pgrep id getent; do need "$cmd"; done
need "$PHP_BIN"
[[ $WEB_USER =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]] || fail 'Invalid WEB_USER account name.'
web_uid=$(id -u "$WEB_USER") || fail "Unknown WEB_USER: $WEB_USER"
[[ $web_uid =~ ^[0-9]+$ && $web_uid != 0 ]] || fail 'WEB_USER must be a non-root account.'
if [[ -z $WEB_GROUP ]]; then WEB_GROUP=$(id -gn "$WEB_USER"); fi
[[ $WEB_GROUP =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]] || fail 'Invalid WEB_GROUP name.'
getent group "$WEB_GROUP" > /dev/null || fail "Unknown WEB_GROUP: $WEB_GROUP"
"$PHP_BIN" -r 'exit(extension_loaded("SimpleXML") ? 0 : 1);' || fail 'PHP SimpleXML is required for update responses.'
for value in "$WAIT_AFTER_MAINTENANCE" "$WAIT_BEFORE_BACKUP" "$WAIT_AFTER_SERVER_START"; do
    [[ $value =~ ^[0-9]+$ && ${#value} -le 5 ]] || fail 'Wait values must be nonnegative integers (at most five digits).'
done
WAIT_AFTER_MAINTENANCE=$((10#$WAIT_AFTER_MAINTENANCE))
WAIT_BEFORE_BACKUP=$((10#$WAIT_BEFORE_BACKUP))
WAIT_AFTER_SERVER_START=$((10#$WAIT_AFTER_SERVER_START))
case $DOWNLOAD_FORMAT in tar.bz2) need tar; need bzip2;; zip) need unzip; need zipinfo;; *) fail 'DOWNLOAD_FORMAT must be tar.bz2 or zip.';; esac
case $CHECKSUM_TYPE in sha256|sha512|md5) need "${CHECKSUM_TYPE}sum";; *) fail 'Unsupported CHECKSUM_TYPE.';; esac
[[ $CHECKSUM_TYPE != md5 ]] || log --warning 'Warning: MD5 only detects accidental corruption; prefer SHA256.'
exec 9>/run/lock/nextcloud-upgrade.lock
flock -n 9 || fail 'Another upgrade is already running.'
[[ ! -e $BACKUP_PATH/upgrade-active ]] || fail "Interrupted attempt recorded in $BACKUP_PATH/upgrade-active; see README.md (Backups and troubleshooting) before retrying."
for path in "$NEXTCLOUD_PATH" "$BACKUP_PATH"; do
    [[ $path == /* && $path != / && -d $path && ! -L $path ]] || fail 'Installation and backup paths must be existing absolute directories, not symlinks.'
    [[ $(realpath -e -- "$path") == "$path" ]] || fail "Use the canonical path: $path"
done
[[ -z ${NEXTCLOUD_CONFIG_DIR:-} ]] || fail 'External configuration directories need a separately reviewed upgrade procedure.'
[[ $BACKUP_PATH != "$NEXTCLOUD_PATH" && $BACKUP_PATH != "$NEXTCLOUD_PATH/"* && $NEXTCLOUD_PATH != "$BACKUP_PATH/"* ]] || fail 'Backup and installation paths overlap.'
[[ $(stat -c %u "$BACKUP_PATH") == 0 ]] || fail 'Backup directory must be owned by root.'
backup_mode=$(stat -c %a "$BACKUP_PATH")
(( (8#$backup_mode & 0022) == 0 )) || fail 'Backup directory must not be writable by group or others.'
[[ ! -e ${NEXTCLOUD_PATH}.old ]] || fail 'Legacy .old directory exists; identify and preserve its recovery state before proceeding.'
[[ -f $NEXTCLOUD_PATH/occ && -f $NEXTCLOUD_PATH/config/config.php ]] || fail 'Nextcloud files are missing.'
data_path=$(realpath -e -- "$(config datadirectory)")
[[ -d $data_path && $data_path != / && $data_path != *[[:cntrl:]]* ]] || fail 'Configured data directory must be an existing directory other than /.'
[[ $data_path != "$NEXTCLOUD_PATH" && $NEXTCLOUD_PATH != "$data_path/"* ]] || fail 'Data directory must not contain the application.'
data_identity=$(stat -c '%d:%i' "$data_path")
[[ $BACKUP_PATH != "$data_path" && $BACKUP_PATH != "$data_path/"* ]] || fail 'Recovery backups must be outside the user-data directory.'
if [[ $data_path == "$NEXTCLOUD_PATH/"* ]]; then
    data_relative=${data_path#"$NEXTCLOUD_PATH/"}
    [[ $data_relative != config && $data_relative != config/* ]] || fail 'Data directory must be separate from configuration.'
fi
# Internal data is pruned from application inspection and ownership operations.
workspace=$(mktemp -d -p /var/tmp nextcloud-upgrade.XXXXXXXX)
log "Private workspace: $workspace"
check_application_tree
current_version=$(occ status --output=json | php_info status off)
[[ $current_version =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail 'Invalid installed version.'
# Read the setting without changing it; an unset key uses Nextcloud's AJAX default.
background_mode=$(occ config:app:get core backgroundjobs_mode --default-value=ajax) || fail 'Cannot read the configured background-job mode.'
case $background_mode in
    cron)
        background_label="cron (external scheduler)"
        if command -v crontab >/dev/null; then
            manage_cron=yes
            background_label="cron (account crontab for $WEB_USER)"
        fi ;;
    webcron) background_label='WebCron (HTTP requests)' ;;
    ajax) background_label='AJAX (page requests)' ;;
    none) background_label='disabled' ;;
    *) fail "Unsupported background-job mode: $background_mode" ;;
esac
check_services
log "Service manager: $SERVICE_MANAGER"
log "Background jobs: $background_label"

if [[ $# == 0 ]]; then
    channel=$(config updater.release.channel || true)
    [[ -z $channel || $channel == stable ]] || fail 'Automatic upgrades require the stable release channel.'
    build=$(php_info build "$NEXTCLOUD_PATH")
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 60 --retry 2 \
        -H 'User-Agent: Nextcloud Updater' "https://updates.nextcloud.com/updater_server/?version=${current_version//./x}xxxstablexx${build}" > "$workspace/update.xml"
    new_version=$(php_info update < "$workspace/update.xml")
    [[ -n $new_version ]] || { log --success 'No stable update is available.'; exit 0; }
else
    new_version=$1
fi
[[ $new_version =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail 'Target must be a numeric stable release.'
download_version=$(awk -F. '{print $1"."$2"."$3}' <<< "$new_version")
current_release=$(awk -F. '{print $1"."$2"."$3}' <<< "$current_version")
[[ $download_version != "$current_release" ]] || { log --success 'Already on the requested release.'; exit 0; }
[[ $(printf '%s\n%s\n' "$current_release" "$download_version" | sort -V | head -n1) == "$current_release" ]] || fail 'Downgrades are not supported.'
current_major=${current_release%%.*}; target_major=${download_version%%.*}
((10#$target_major <= 10#$current_major + 1)) || fail 'Cannot skip major versions.'
if [[ $# == 1 && $target_major != "$current_major" ]]; then
    fail 'Use automatic discovery for major upgrades so the update server selects an eligible upgrade path.'
fi
archive_name="nextcloud-$download_version.$DOWNLOAD_FORMAT"
url="https://download.nextcloud.com/server/releases/$archive_name"
log "Downloading stable release $download_version"
for suffix in '' ".$CHECKSUM_TYPE"; do
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 1800 --retry 2 \
        "$url$suffix" -o "$workspace/$archive_name$suffix.part"
    mv -T -- "$workspace/$archive_name$suffix.part" "$workspace/$archive_name$suffix"
done
expected=$(awk -v name="$archive_name" '$2 == name || $2 == "*"name { print $1 }' "$workspace/$archive_name.$CHECKSUM_TYPE")
actual=$("${CHECKSUM_TYPE}sum" "$workspace/$archive_name"); actual=${actual%% *}
[[ $expected == "$actual" ]] || fail 'Archive checksum failed or checksum entry is ambiguous.'
log --success 'Downloaded archive checksum verified.'

check_space() {
    local path=$1 bytes=$2 inodes=$3 available free_inodes
    available=$(df -PB1 -- "$path" | awk 'END {print $4}')
    free_inodes=$(df -Pi -- "$path" | awk 'END {print $4}')
    [[ $available =~ ^[0-9]+$ && $free_inodes =~ ^[0-9]+$ ]] || fail "Cannot read filesystem capacity: $path"
    (( available >= bytes && free_inodes >= inodes )) || fail "Insufficient space or inodes at $path (need $bytes bytes, $inodes inodes)."
}
# Archive links must name a regular file in the same directory. This permits
# shipped asset/license links without allowing a link to redirect extraction.
if [[ $DOWNLOAD_FORMAT == tar.bz2 ]]; then
    # Two inspection scans precede extraction; each scan decompresses tar.bz2.
    with_progress 'Archive pass 1/3: reading file paths (full decompression)' tar -tjf "$workspace/$archive_name" > "$workspace/archive.names"
    with_progress 'Archive pass 2/3: reading file types, links and sizes (full decompression)' tar -tvjf "$workspace/$archive_name" > "$workspace/archive.details"
    awk '
        {t=substr($1,1,1); types[$6]=t}
        t!="-" && t!="d" && t!="l" {bad=1}
        t=="l" {
            if(NF!=8 || $7!="->" || $8!~/^[A-Za-z0-9_.-]+$/ || $8=="." || $8=="..") bad=1
            parent=$6; sub(/[^/]+$/, "", parent); targets[$6]=parent $8; nlinks++
        }
        END {
            for(link in targets) {
                target=targets[link]; steps=0
                while(types[target]=="l" && steps++<nlinks) target=targets[target]
                if(types[target]!="-") bad=1
            }
            exit bad
        }
    ' "$workspace/archive.details" || fail 'Archive contains unsafe links or special files.'
    read -r unpacked_bytes entry_count < <(awk '{bytes+=$3;n++} END {printf "%.0f %d\n",bytes,n}' "$workspace/archive.details")
else
    with_progress 'Reading ZIP archive paths' unzip -Z1 "$workspace/$archive_name" > "$workspace/archive.names"
    with_progress 'Reading ZIP archive file types, links and sizes' zipinfo -l "$workspace/$archive_name" > "$workspace/archive.details"
    awk '/^[bcps]/ {exit 1} /^l/ {print $NF}' "$workspace/archive.details" > "$workspace/archive.links" || fail 'ZIP contains special files.'
    : > "$workspace/archive.link-targets"
    while IFS= read -r link; do
        [[ $link =~ ^nextcloud/[A-Za-z0-9_./-]+$ ]] || fail 'Unsupported ZIP link name.'
        target=$(unzip -p "$workspace/$archive_name" "$link")
        [[ $target =~ ^[A-Za-z0-9_.-]+$ && $target != . && $target != .. ]] || fail 'ZIP link must name a file in the same directory.'
        target=${link%/*}/$target
        printf '%s\t%s\n' "$link" "$target" >> "$workspace/archive.link-targets"
    done < "$workspace/archive.links"
    awk '
        FILENAME==ARGV[1] {types[$NF]=substr($1,1,1);next}
        {targets[$1]=$2;nlinks++}
        END {
            for(link in targets) {
                target=targets[link]; steps=0
                while(types[target]=="l" && steps++<nlinks) target=targets[target]
                if(types[target]!="-") exit 1
            }
        }
    ' "$workspace/archive.details" "$workspace/archive.link-targets" || fail 'ZIP links must resolve to regular archive files.'
    read -r unpacked_bytes entry_count < <(awk '/^[-d]/ {bytes+=$4;n++} END {printf "%.0f %d\n",bytes,n}' "$workspace/archive.details")
fi
awk '$0 !~ /^nextcloud(\/|$)/ || $0 ~ /(^|\/)\.\.(\/|$)/ || $0 ~ /\\/ {exit 1}' "$workspace/archive.names" || fail 'Unsafe archive paths.'
(( unpacked_bytes > 0 && entry_count > 0 )) || fail 'Empty release archive.'
check_space "$workspace" "$((unpacked_bytes + 104857600))" "$((entry_count + 1000))"
log --success 'Archive paths, file types, link targets and extraction space checked.'
if [[ $DOWNLOAD_FORMAT == tar.bz2 ]]; then
    with_progress 'Archive pass 3/3: extracting the validated release' tar --no-same-owner -xjf "$workspace/$archive_name" -C "$workspace"
else
    with_progress 'Extracting the validated ZIP release' unzip -q "$workspace/$archive_name" -d "$workspace"
fi
release="$workspace/nextcloud"
for file in occ version.php index.php status.php lib/versioncheck.php; do [[ -f $release/$file ]] || fail "Release missing $file"; done
find "$release" -type l -print0 > "$workspace/release.links"
with_progress 'Checking extracted file links' check_file_links "$release" "$workspace/release.links"
log 'Checking the extracted release version, stable channel and PHP compatibility.'
php_info release "$release" "$download_version"
log --success 'Stable release and PHP compatibility checked.'
# Stage on the installation filesystem so both replacements are renames, not copies.
parent=$(dirname -- "$NEXTCLOUD_PATH")
log 'Checking staging space and existing application size.'
app_bytes=$(find_application -printf '%s\n' | awk '{bytes+=$1} END {printf "%.0f\n",bytes}')
app_inodes=$(find_application -printf '.\n' | wc -l)
check_space "$parent" "$((unpacked_bytes + app_bytes + 104857600))" "$((entry_count + app_inodes + 1000))"
stage=$(mktemp -d -p "$parent" .nextcloud-stage.XXXXXXXX)
with_progress 'Copying the release into staging' cp -a -- "$release" "$stage/nextcloud"
fetch_config_originals
prepare_local_files inspect
if [[ -n $data_relative ]]; then
    # Some archives contain the standard data/ skeleton. It is not live data.
    if [[ $data_relative == data && -d $stage/nextcloud/data ]]; then
        rm -rf -- "$stage/nextcloud/data"
    fi
    [[ ! -e $stage/nextcloud/$data_relative ]] || fail 'Internal data directory conflicts with new release content.'
    data_parent=$(dirname -- "$stage/nextcloud/$data_relative")
    mkdir -p -- "$data_parent"
fi
occ config:list system --output=json | php_info apps "$NEXTCLOUD_PATH" > "$workspace/extra-app-paths"
check_custom_apps

# Resolve optional copies before maintenance; never merge over release content.
while IFS= read -r apps_dir; do
    add_preserved_path "$apps_dir"
done < "$workspace/extra-app-paths"
for app in "$NEXTCLOUD_PATH/apps/"*; do
    [[ -d $app ]] || continue
    [[ ! -e $stage/nextcloud/apps/${app##*/} ]] || continue
    if [[ -f $app/appinfo/info.xml ]]; then
        app_kind=$(php_info custom-app "$app")
        if [[ $app_kind == no ]]; then
            log "Not carrying removed shipped app: ${app##*/}"
            continue
        fi
    fi
    add_preserved_path "apps/${app##*/}"
done
for theme in "$NEXTCLOUD_PATH/themes/"*; do
    [[ -e $theme ]] || continue
    [[ ${theme##*/} == README || ${theme##*/} == example ]] || add_preserved_path "themes/${theme##*/}"
done
if [[ -n $PRESERVE_PATHS_FILE ]]; then
    [[ -f $PRESERVE_PATHS_FILE && -r $PRESERVE_PATHS_FILE ]] || fail 'PRESERVE_PATHS_FILE must be a readable regular file.'
    while IFS= read -r path || [[ -n $path ]]; do
        [[ -n $path && $path != \#* ]] || continue
        add_preserved_path "$path"
    done < "$PRESERVE_PATHS_FILE"
fi
: > "$workspace/preserved-paths"
for path in "${preserved_paths[@]}"; do
    printf '%s\n' "$path" >> "$workspace/preserved-paths"
    log "Preserving: $path"
done
preserve_bytes=0
for path in config "${preserved_paths[@]}"; do
    bytes=$(du -sb -- "$NEXTCLOUD_PATH/$path" | awk '{print $1}')
    preserve_bytes=$((preserve_bytes + bytes))
done

# Prepare credentials privately, retaining neither password nor credential file in logs.
db_type=$(config dbtype); db_name=$(config dbname); db_user=$(config dbuser)
db_password=$(config dbpassword); db_host=$(config dbhost)
[[ -n $db_name && $db_name != -* ]] || fail 'Invalid database name.'
if [[ $db_type == mysql ]]; then
    if command -v mariadb-dump >/dev/null; then dump_cmd=mariadb-dump; else need mysqldump; dump_cmd=mysqldump; fi
    if command -v mariadb >/dev/null; then db_cmd=mariadb; else need mysql; db_cmd=mysql; fi
    credentials="$workspace/database.cnf"
    option_value() {
        local value=$1
        [[ $value != *$'\n'* && $value != *$'\r'* ]] || fail 'Multiline database option unsupported.'
        value=${value//\\/\\\\}; value=${value//\"/\\\"}
        printf '"%s"' "$value"
    }
    {
        printf '[client]\nuser='; option_value "$db_user"
        printf '\npassword='; option_value "$db_password"
        if [[ $db_host =~ ^([^:]+):([0-9]+)$ ]]; then
            printf '\nhost='; option_value "${BASH_REMATCH[1]}"
            printf '\nport=%s' "${BASH_REMATCH[2]}"
        elif [[ $db_host == localhost:/* ]]; then
            printf '\nhost=localhost\nsocket='; option_value "${db_host#localhost:}"
        else
            printf '\nhost='; option_value "$db_host"
        fi
        printf '\n'
    } > "$credentials"
    db_bytes=$("$db_cmd" --defaults-file="$credentials" --batch --skip-column-names --database="$db_name" \
        --execute='SELECT COALESCE(SUM(DATA_LENGTH+INDEX_LENGTH),0) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')
elif [[ $db_type == pgsql ]]; then
    need pg_dump; need psql
    db_bytes=$(PGPASSWORD="$db_password" psql --no-psqlrc --host="$db_host" --username="$db_user" --dbname="$db_name" --tuples-only --no-align --command='SELECT pg_database_size(current_database())')
else
    fail "Unsupported database type: $db_type"
fi
[[ $db_bytes =~ ^[0-9]+$ ]] || fail 'Cannot estimate database backup size.'
if [[ -n $BACKUP_SSH_DEST ]]; then
    need ssh; need tar
    [[ $BACKUP_SSH_DEST =~ ^([A-Za-z0-9_][A-Za-z0-9_.@-]*):(/.*)$ && $BACKUP_SSH_DEST != *[[:cntrl:]]* ]] || fail 'BACKUP_SSH_DEST must be [user@]host:/absolute/directory; use an SSH host alias for custom ports or IPv6.'
    ssh_host=${BASH_REMATCH[1]}; ssh_directory=${BASH_REMATCH[2]%/}
    [[ -n $ssh_directory ]] || fail 'Use a dedicated remote backup directory, not /.'
    quoted_directory=$(shell_quote "$ssh_directory")
    backup_ssh "command -v tar > /dev/null && command -v sync > /dev/null && test -d $quoted_directory && test -w $quoted_directory" || fail 'Cannot access SSH backup destination; check root SSH access, known_hosts and directory permissions.'
    log "Off-site recovery backup enabled: $BACKUP_SSH_DEST"
fi
# SQL can be larger than table storage; reserve raw + compressed dump headroom.
check_backup_space() {
    local bytes=$((db_bytes * 5 + preserve_bytes + data_bytes + 104857600))
    local inodes=$((app_inodes + data_inodes + 1000))
    # Packaging keeps the directory copy too; allow tar headers/ACLs per entry.
    if [[ $BACKUP_ARCHIVE == yes ]]; then bytes=$((bytes * 2 + inodes * 4096)); fi
    # The pending config/custom copies into staging also need room on a shared FS.
    if [[ $(stat -c %d "$BACKUP_PATH") == "$(stat -c %d "$stage")" ]]; then
        bytes=$((bytes + preserve_bytes)); inodes=$((inodes + app_inodes))
    fi
    check_space "$BACKUP_PATH" "$bytes" "$inodes"
}
data_bytes=0 data_inodes=0
if [[ $BACKUP_DATA == yes ]]; then
    occ config:list system --output=json | php_info local-data
    data_bytes=$(du -sb "$data_path" | awk '{print $1}')
    data_inodes=$(find "$data_path" -printf '.\n' | wc -l)
    [[ $data_bytes =~ ^[0-9]+$ && $data_inodes =~ ^[0-9]+$ ]] || fail 'Cannot estimate data backup size.'
    log "Data backup enabled: $data_path -> $BACKUP_PATH (estimated $data_bytes bytes)."
else
    log 'Data backup disabled for this run.'
    # Best-effort advice only: a filesystem type cannot reveal backup coverage.
    data_fstype=$(findmnt --noheadings --raw --target "$data_path" --output FSTYPE 2>/dev/null || true)
    case $data_fstype in
        ext[234]|xfs|btrfs|zfs|f2fs|bcachefs|tmpfs|ramfs|overlay|aufs|fuseblk)
            log --warning 'Note: data appears to be on local storage. Consider a separate data backup if your existing backup system does not cover it.' ;;
    esac
fi
if [[ -z $BACKUP_SSH_DEST && $(stat -c %d "$BACKUP_PATH") == "$(stat -c %d "$data_path")" ]]; then
    log --warning 'Note: recovery backups share the data filesystem. Consider an off-server copy if you do not already keep one.'
fi
check_backup_space
if [[ $manage_cron == yes ]]; then
    read_cron "$workspace/cron.original" || fail "Cannot read $WEB_USER crontab; configure or inspect it before upgrading."
    cron_was_present=$cron_read_present
    if [[ $cron_was_present == no ]]; then
        log --warning "No account crontab for $WEB_USER. Check that any systemd timers or external workers are paused for the upgrade."
    fi
    # Match this installation only. Reject an unrecognised cron.php invocation.
    awk -v target="$NEXTCLOUD_PATH/cron.php" '
        /^[[:space:]]*#/ {print;next}
        /cron\.php/ {
            found=0; for(i=1;i<=NF;i++) if($i==target) found=1
            if(!found) exit 1
            print "# nextcloud-upgrade: " $0; next
        }
        {print}
    ' "$workspace/cron.original" > "$workspace/cron.disabled" || fail 'Unrecognised cron.php invocation; inspect the crontab first.'
elif [[ $background_mode == cron ]]; then
    log --warning 'Cron mode is selected but crontab is unavailable. Pause system timers and external schedulers before proceeding.'
fi
log --success "Ready: $current_version -> $download_version. Preparation checks passed."
printf '  %-18s %s\n' \
    'Installation:' "$NEXTCLOUD_PATH" \
    'Run as:' "$WEB_USER:$WEB_GROUP ($PHP_BIN)" \
    'Services:' "$SERVICE_MANAGER; web=$WEB_SERVICE; PHP=$PHP_SERVICE" \
    'Background jobs:' "$background_label" \
    'Settling pause:' "${WAIT_AFTER_MAINTENANCE}s before service shutdown" \
    'Database:' "$db_type / $db_name" \
    'Data directory:' "$data_path" \
    'Backup directory:' "$BACKUP_PATH" \
    'Data backup:' "$BACKUP_DATA" \
    'Local bundle:' "$BACKUP_ARCHIVE" \
    'SSH copy:' "${BACKUP_SSH_DEST:-disabled}"
log --question 'Proceed with the upgrade? [y/N]:'
read -r answer
[[ $answer == y || $answer == Y ]] || { log 'Upgrade cancelled.'; exit 0; }

# Name each recovery set after its source version, including the build component.
recovery=$(mktemp -d -p "$BACKUP_PATH" "upgrade-${current_version}-$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
old_path="${NEXTCLOUD_PATH}.old.${recovery##*/}"
if [[ $manage_cron == yes ]]; then
    cp -- "$workspace/cron.original" "$workspace/cron.disabled" "$recovery/"
fi
cp -- "$workspace/preserved-paths" "$recovery/preserved-paths"
printf 'installation=%s\nprevious_version=%s\ntarget_version=%s\nold_application=%s\nstaging=%s\nworkspace=%s\ndatabase_type=%s\ndatabase_name=%s\nservice_manager=%s\nweb_service=%s\nphp_service=%s\n' \
    "$NEXTCLOUD_PATH" "$current_version" "$download_version" "$old_path" "$stage" "$workspace" "$db_type" "$db_name" "$SERVICE_MANAGER" "$WEB_SERVICE" "$PHP_SERVICE" > "$recovery/manifest"
{
    printf 'data_directory=%s\ndata_identity=%s\ninternal_data_relative=%s\nbackup_data=%s\n' "$data_path" "$data_identity" "$data_relative" "$BACKUP_DATA"
    printf 'backup_archive=%s\nbackup_ssh_destination=%s\n' "$BACKUP_ARCHIVE" "$BACKUP_SSH_DEST"
    printf 'web_user=%s\nweb_group=%s\nphp_binary=%s\n' "$WEB_USER" "$WEB_GROUP" "$PHP_BIN"
    printf 'background_jobs_mode=%s\naccount_crontab_managed=%s\naccount_crontab_present=%s\n' "$background_mode" "$manage_cron" "$cron_was_present"
} >> "$recovery/manifest"
set_phase quiescing 'Enabling maintenance and suspending background jobs.'
printf '%s\n' "$recovery" > "$BACKUP_PATH/upgrade-active"
sync -f "$BACKUP_PATH"
production_changed=yes
offline_started=$SECONDS
occ maintenance:mode --on
# HTTP job modes stop with maintenance/web services; only cron mode edits crontab.
if [[ $manage_cron == yes ]]; then
    # Verify no operator changed cron between preflight and this write.
    read_cron "$recovery/cron.current"
    cmp -s "$recovery/cron.original" "$recovery/cron.current" || fail 'Crontab changed since preflight.'
    if [[ $cron_was_present == yes ]]; then
        cron_changed=yes
        crontab -u "$WEB_USER" "$recovery/cron.disabled"
    fi
fi
# Let in-flight HTTP requests finish before asking the service manager to stop.
if (( WAIT_AFTER_MAINTENANCE > 0 )); then
    with_progress "Allowing ${WAIT_AFTER_MAINTENANCE}s for active requests to finish before service shutdown" sleep "$WAIT_AFTER_MAINTENANCE"
fi
log "Stopping application services through $SERVICE_MANAGER."
stop_services
# Maintenance prevents new jobs; an already running CLI job must also finish.
for ((elapsed=0; ; elapsed++)); do
    if pgrep -u "$WEB_USER" -f '(^|/)(cron\.php|occ)([[:space:]]|$)' > "$recovery/background-pids"; then
        ((elapsed < WAIT_BEFORE_BACKUP)) || fail 'Existing cron/occ process did not finish; do not kill it blindly.'
        sleep 1
    else
        rc=$?; [[ $rc == 1 ]] || fail 'Cannot inspect background processes.'
        break
    fi
done
# Recheck after shutdown in case a selected source or mount changed since preflight.
check_application_tree
check_custom_apps
for path in "${preserved_paths[@]}"; do check_preserved_path "$path"; done
[[ $(realpath -e -- "$(config datadirectory)") == "$data_path" && -d $data_path && $(stat -c '%d:%i' "$data_path") == "$data_identity" ]] || fail 'Configured data directory or its mount changed during preparation.'
if [[ $BACKUP_DATA == yes ]]; then
    data_bytes=$(du -sb "$data_path" | awk '{print $1}')
    data_inodes=$(find "$data_path" -printf '.\n' | wc -l)
    check_backup_space
fi
set_phase backup 'Creating and validating recovery backups.'
if [[ $db_type == mysql ]]; then
    "$dump_cmd" --defaults-file="$credentials" --single-transaction --quick --default-character-set=utf8mb4 --comments --dump-date "$db_name" > "$recovery/database.sql.part"
    tail -n 5 "$recovery/database.sql.part" | grep -q '^-- Dump completed on ' || fail 'Database dump completion marker missing.'
else
    PGPASSWORD="$db_password" pg_dump --host="$db_host" --username="$db_user" --dbname="$db_name" --file="$recovery/database.sql.part"
    tail -n 10 "$recovery/database.sql.part" | grep -q '^-- PostgreSQL database dump complete' || fail 'Database dump completion marker missing.'
fi
[[ -s $recovery/database.sql.part ]] || fail 'Database dump is empty.'
gzip -c "$recovery/database.sql.part" > "$recovery/database.sql.gz.part"
gzip -t "$recovery/database.sql.gz.part"
mv -T -- "$recovery/database.sql.gz.part" "$recovery/database.sql.gz"
rm -- "$recovery/database.sql.part"
[[ -z $credentials ]] || rm -f -- "$credentials"
credentials=; unset db_password
cp -a -- "$NEXTCLOUD_PATH/config" "$recovery/config"
for file in .htaccess .user.ini; do
    if [[ -f $NEXTCLOUD_PATH/$file ]]; then cp -aL -- "$NEXTCLOUD_PATH/$file" "$recovery/$file"; fi
done
for path in "${preserved_paths[@]}"; do
    mkdir -p -- "$(dirname -- "$recovery/custom/$path")"
    cp -aT -- "$NEXTCLOUD_PATH/$path" "$recovery/custom/$path"
done
if [[ $BACKUP_DATA == yes ]]; then backup_data; fi
set_phase backups-ready 'Recovery backups are ready; application replacement has not started.' --success
if [[ $BACKUP_ARCHIVE == yes ]]; then package_backups; fi
if [[ -n $BACKUP_SSH_DEST ]]; then copy_backups_offsite; fi
# Reclassify after quiescing: config or overrides may have changed since preflight.
prepare_local_files apply
for path in "${preserved_paths[@]}"; do
    check_preserved_path "$path"
    mkdir -p -- "$(dirname -- "$stage/nextcloud/$path")"
    cp -aT -- "$NEXTCLOUD_PATH/$path" "$stage/nextcloud/$path"
done
find "$stage/nextcloud" -type l -print0 > "$workspace/staged.links"
check_file_links "$stage/nextcloud" "$workspace/staged.links"
chown -hR "$WEB_USER:$WEB_GROUP" "$stage/nextcloud"
find "$stage/nextcloud" -type d -exec chmod 750 {} +
find "$stage/nextcloud" -type f -exec chmod 640 {} +
[[ $(stat -c %d "$NEXTCLOUD_PATH") == "$(stat -c %d "$stage/nextcloud")" ]] || fail 'Replacement must remain on the same filesystem.'
sync -f "$recovery"
set_phase replacing 'Replacing application with same-filesystem renames.'
replacement_started=yes
mv -T -- "$NEXTCLOUD_PATH" "$old_path"
mv -T -- "$stage/nextcloud" "$NEXTCLOUD_PATH"
if [[ -n $data_relative ]]; then
    mv -T -- "$old_path/$data_relative" "$NEXTCLOUD_PATH/$data_relative"
fi
sync -f "$parent"
set_phase migration-started 'Starting database migration; automatic application rollback ends here.'
migration_started=yes
# Record this boundary before launching a command that can change the database.
occ upgrade --no-interaction 2>&1 | tee "$recovery/upgrade.log"
set_phase verifying 'Checking installed version, database migration state, and maintenance mode.'
# occ may change maintenance itself; application services remain stopped.
occ maintenance:mode --on
occ status --output=json | php_info status on "$download_version" > "$recovery/status.version"
set_phase restoring 'Starting application services under maintenance, then restoring background jobs.'
start_services
restore_cron
occ maintenance:mode --off
occ status --output=json | php_info status off "$download_version" > "$recovery/status.version"
# Keep the final version check after services and background jobs resume.
set_phase complete 'Version, migration status and service checks passed.' --success
rm -- "$BACKUP_PATH/upgrade-active"
sync -f "$BACKUP_PATH"
finished=yes
log --success "Upgrade complete: $current_release -> $download_version. Elapsed $((SECONDS - upgrade_started))s; maintenance window $((SECONDS - offline_started))s."
printf '  %-18s %s\n' 'Recovery directory:' "$recovery" 'Previous code:' "$old_path" 'Data directory:' "$data_path"
if [[ $BACKUP_ARCHIVE == yes ]]; then log "Restore bundle: $recovery/backup.tar (checksum: backup.tar.sha256)"; fi
if [[ -n $BACKUP_SSH_DEST ]]; then log "Off-server archive: $ssh_host:$ssh_directory/${recovery##*/}.tar"; fi
log 'Next: check the web interface, file upload/download and client sync; review Administration Overview and re-enable compatible apps.'
