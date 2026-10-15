#!/bin/bash
# Upgrade one conventional Nextcloud installation. See README.md for usage and recovery.
set -Eeuo pipefail
umask 077

NEXTCLOUD_PATH="${NEXTCLOUD_PATH:-/var/www/nextcloud}"
BACKUP_PATH="${BACKUP_PATH:-/var/backups/nextcloud}"
SERVICE_MANAGER="${SERVICE_MANAGER:-monit}"
WEB_SERVICE="${WEB_SERVICE:-nginx}"
# Use 'none' only when PHP runs inside the web-server process (e.g. mod_php).
PHP_SERVICE="${PHP_SERVICE:-php8.4-fpm}"
managed_services=("$WEB_SERVICE")
if [[ $PHP_SERVICE != none ]]; then
    managed_services+=("$PHP_SERVICE")
fi
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
finished=no

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
fail() { log --error "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required command missing: $1"; }
occ() { (cd "$NEXTCLOUD_PATH" && sudo -u www-data php ./occ "$@"); }
config() { occ config:system:get "$1"; }

find_application() {
    if [[ -n $data_relative ]]; then
        find "$NEXTCLOUD_PATH" -samefile "$data_path" -prune -o "$@"
    else
        find "$NEXTCLOUD_PATH" "$@"
    fi
}

check_application_tree() {
    local mounts mount unsupported
    mounts=$(findmnt --raw --noheadings --output TARGET)
    while IFS= read -r mount; do
        printf -v mount '%b' "$mount" # findmnt --raw escapes spaces and backslashes.
        [[ $mount != "$NEXTCLOUD_PATH" && $mount != "$NEXTCLOUD_PATH/"* ]] || fail "Mount inside application tree: $mount"
    done <<< "$mounts"
    unsupported=$(find_application ! -type d ! -type f -print -quit)
    [[ -z $unsupported ]] || fail "Only regular files and directories are supported in the application tree (no symlinks): $unsupported"
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
    local entries=(config database.sql.gz manifest preserved-paths cron.original cron.disabled phase)
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
    php -r "$(cat <<'PHP'
function bad($message) { fwrite(STDERR, $message . "\n"); exit(1); }
$mode = $argv[1];
if ($mode === 'build') {
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
restore_cron() {
    [[ $cron_changed == yes ]] || return 0
    crontab -u www-data -l > "$recovery/cron.current" || return 1
    if cmp -s "$recovery/cron.original" "$recovery/cron.current"; then
        cron_changed=no
        return 0
    fi
    cmp -s "$recovery/cron.disabled" "$recovery/cron.current" || {
        log --warning 'Crontab changed externally; reconcile cron.original and cron.current manually.' >&2
        return 1
    }
    crontab -u www-data "$recovery/cron.original" || return 1
    cron_changed=no
}

on_exit() {
    local rc=$? safe=yes
    trap - EXIT ERR INT TERM HUP
    set +e
    [[ -z $credentials ]] || rm -f -- "$credentials"
    if [[ $rc != 0 ]]; then
        log --error "FAILED (exit $rc). No successful upgrade is being reported." >&2
        if [[ $production_changed == yes ]]; then
            # Re-establish the marker even if the final marker removal/sync failed.
            printf '%s\n' "$recovery" > "$BACKUP_PATH/upgrade-active"
            sync -f "$BACKUP_PATH"
            printf 'exit=%s\nphase=%s\nmigration_started=%s\n' "$rc" "$phase" "$migration_started" > "$recovery/failure"

            if [[ $migration_started == yes ]]; then
                # Service restoration may already have restored cron. Re-disable only
                # if it is still exactly one of our known snapshots.
                if crontab -u www-data -l > "$recovery/cron.current"; then
                    if cmp -s "$recovery/cron.original" "$recovery/cron.current"; then
                        crontab -u www-data "$recovery/cron.disabled" || log --error 'Disable Nextcloud cron manually now.' >&2
                    elif ! cmp -s "$recovery/cron.disabled" "$recovery/cron.current"; then
                        log --warning 'Crontab changed externally; disable Nextcloud cron manually now.' >&2
                    fi
                else
                    log --error 'Cannot inspect crontab; disable Nextcloud cron manually now.' >&2
                fi
                # Code alone cannot undo a database migration. Keep every artifact.
                occ maintenance:mode --on >/dev/null 2>&1
                stop_services || log --error 'Could not confirm services stopped; block access manually now.' >&2
                log --warning 'Database migration may have changed state. Keep cron disabled; see README.md (Backups and troubleshooting) for manual recovery.' >&2
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
for cmd in cat sudo php curl mktemp flock realpath stat find findmnt df du awk grep sort head tail wc dirname date cmp cp mv rm chmod chown mkdir gzip tee sync crontab systemctl pgrep; do need "$cmd"; done
php -r 'exit(extension_loaded("SimpleXML") ? 0 : 1);' || fail 'PHP SimpleXML is required for update responses.'
for value in "$WAIT_BEFORE_BACKUP" "$WAIT_AFTER_SERVER_START"; do
    [[ $value =~ ^[0-9]+$ && ${#value} -le 5 ]] || fail 'Wait values must be nonnegative integers (at most five digits).'
done
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
check_application_tree
current_version=$(occ status --output=json | php_info status off)
[[ $current_version =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail 'Invalid installed version.'
check_services
log "Service manager: $SERVICE_MANAGER"

workspace=$(mktemp -d -p /var/tmp nextcloud-upgrade.XXXXXXXX)
log "Private workspace: $workspace"
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
# Inspect names and entry types before extraction; reject links and special files.
if [[ $DOWNLOAD_FORMAT == tar.bz2 ]]; then
    tar -tjf "$workspace/$archive_name" > "$workspace/archive.names"
    tar -tvjf "$workspace/$archive_name" > "$workspace/archive.details"
    awk 'substr($1,1,1)!="-" && substr($1,1,1)!="d" {exit 1}' "$workspace/archive.details" || fail 'Archive contains links or special files.'
    read -r unpacked_bytes entry_count < <(awk '{bytes+=$3;n++} END {printf "%.0f %d\n",bytes,n}' "$workspace/archive.details")
else
    unzip -Z1 "$workspace/$archive_name" > "$workspace/archive.names"
    zipinfo -l "$workspace/$archive_name" > "$workspace/archive.details"
    awk '/^[bclps]/ {exit 1}' "$workspace/archive.details" || fail 'ZIP contains links or special files.'
    read -r unpacked_bytes entry_count < <(awk '/^[-d]/ {bytes+=$4;n++} END {printf "%.0f %d\n",bytes,n}' "$workspace/archive.details")
fi
awk '$0 !~ /^nextcloud(\/|$)/ || $0 ~ /(^|\/)\.\.(\/|$)/ || $0 ~ /\\/ {exit 1}' "$workspace/archive.names" || fail 'Unsafe archive paths.'
(( unpacked_bytes > 0 && entry_count > 0 )) || fail 'Empty release archive.'
check_space "$workspace" "$((unpacked_bytes + 104857600))" "$((entry_count + 1000))"
if [[ $DOWNLOAD_FORMAT == tar.bz2 ]]; then
    tar --no-same-owner -xjf "$workspace/$archive_name" -C "$workspace"
else
    unzip -q "$workspace/$archive_name" -d "$workspace"
fi
release="$workspace/nextcloud"
for file in occ version.php index.php status.php lib/versioncheck.php; do [[ -f $release/$file ]] || fail "Release missing $file"; done
release_links=$(find "$release" -type l -print -quit)
[[ -z $release_links ]] || fail 'Release contains symlinks.'
php_info release "$release" "$download_version"
log --success 'Stable release and PHP compatibility checked.'
# Stage on the installation filesystem so both replacements are renames, not copies.
parent=$(dirname -- "$NEXTCLOUD_PATH")
app_bytes=$(find_application -printf '%s\n' | awk '{bytes+=$1} END {printf "%.0f\n",bytes}')
app_inodes=$(find_application -printf '.\n' | wc -l)
check_space "$parent" "$((unpacked_bytes + app_bytes + 104857600))" "$((entry_count + app_inodes + 1000))"
stage=$(mktemp -d -p "$parent" .nextcloud-stage.XXXXXXXX)
cp -a -- "$release" "$stage/nextcloud"
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
crontab -u www-data -l > "$workspace/cron.original" || fail 'Cannot read www-data crontab; configure or inspect it before upgrading.'
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
log --success "Ready: $current_version -> $download_version. Database and config backup will follow quiescence."
log --question 'Proceed with the upgrade? [y/N]:'
read -r answer
[[ $answer == y || $answer == Y ]] || { log 'Upgrade cancelled.'; exit 0; }

recovery=$(mktemp -d -p "$BACKUP_PATH" "upgrade-$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
old_path="${NEXTCLOUD_PATH}.old.${recovery##*/}"
cp -- "$workspace/cron.original" "$workspace/cron.disabled" "$recovery/"
cp -- "$workspace/preserved-paths" "$recovery/preserved-paths"
printf 'installation=%s\nprevious_version=%s\ntarget_version=%s\nold_application=%s\nstaging=%s\nworkspace=%s\ndatabase_type=%s\ndatabase_name=%s\nservice_manager=%s\nweb_service=%s\nphp_service=%s\n' \
    "$NEXTCLOUD_PATH" "$current_version" "$download_version" "$old_path" "$stage" "$workspace" "$db_type" "$db_name" "$SERVICE_MANAGER" "$WEB_SERVICE" "$PHP_SERVICE" > "$recovery/manifest"
printf 'data_directory=%s\ndata_identity=%s\ninternal_data_relative=%s\nbackup_data=%s\n' "$data_path" "$data_identity" "$data_relative" "$BACKUP_DATA" >> "$recovery/manifest"
printf 'backup_archive=%s\nbackup_ssh_destination=%s\n' "$BACKUP_ARCHIVE" "$BACKUP_SSH_DEST" >> "$recovery/manifest"
set_phase quiescing "Enabling maintenance, disabling Nextcloud cron, and stopping application services through $SERVICE_MANAGER."
printf '%s\n' "$recovery" > "$BACKUP_PATH/upgrade-active"
sync -f "$BACKUP_PATH"
production_changed=yes
occ maintenance:mode --on
# Verify no operator changed cron between preflight and this write.
crontab -u www-data -l > "$recovery/cron.current"
cmp -s "$recovery/cron.original" "$recovery/cron.current" || fail 'Crontab changed since preflight.'
cron_changed=yes
crontab -u www-data "$recovery/cron.disabled"
stop_services
# Maintenance prevents new jobs; an already running CLI job must also finish.
for ((elapsed=0; ; elapsed++)); do
    if pgrep -u www-data -f '(^|/)(cron\.php|occ)([[:space:]]|$)' > "$recovery/background-pids"; then
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
for path in "${preserved_paths[@]}"; do
    mkdir -p -- "$(dirname -- "$recovery/custom/$path")"
    cp -aT -- "$NEXTCLOUD_PATH/$path" "$recovery/custom/$path"
done
if [[ $BACKUP_DATA == yes ]]; then backup_data; fi
set_phase backups-ready 'Recovery backups are ready; application replacement has not started.' --success
if [[ $BACKUP_ARCHIVE == yes ]]; then package_backups; fi
if [[ -n $BACKUP_SSH_DEST ]]; then copy_backups_offsite; fi
# Keep all config fragments and known installation-specific application components.
rm -rf -- "$stage/nextcloud/config"
cp -a -- "$NEXTCLOUD_PATH/config" "$stage/nextcloud/config"
for path in "${preserved_paths[@]}"; do
    check_preserved_path "$path"
    mkdir -p -- "$(dirname -- "$stage/nextcloud/$path")"
    cp -aT -- "$NEXTCLOUD_PATH/$path" "$stage/nextcloud/$path"
done
for file in .htaccess .user.ini; do
    [[ ! -f $NEXTCLOUD_PATH/$file ]] || cp -a -- "$NEXTCLOUD_PATH/$file" "$stage/nextcloud/$file"
done
chown -R www-data:www-data "$stage/nextcloud"
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
# occ may change maintenance itself; services and cron are still stopped.
occ maintenance:mode --on
occ status --output=json | php_info status on "$download_version" > "$recovery/status.version"
set_phase restoring 'Starting application services under maintenance, then restoring cron.'
start_services
restore_cron
occ maintenance:mode --off
occ status --output=json | php_info status off "$download_version" > "$recovery/status.version"
# Services were already verified active. A browser/client check remains mandatory.
set_phase complete 'Local upgrade checks passed. Verify the web interface and a client before removing recovery artifacts.' --success
rm -- "$BACKUP_PATH/upgrade-active"
sync -f "$BACKUP_PATH"
finished=yes
log "Recovery retained: $recovery and $old_path"
