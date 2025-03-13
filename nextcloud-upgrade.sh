#!/bin/bash

set -eo pipefail

# Default values can be overridden by environment variables
NEXTCLOUD_PATH="${NEXTCLOUD_PATH:-/var/www/nextcloud}"
BACKUP_PATH="${BACKUP_PATH:-/var/backups/nextcloud}"  # use an absolute backup path
WEB_SERVICE="${WEB_SERVICE:-nginx}"
WAIT_BEFORE_BACKUP="${WAIT_BEFORE_BACKUP:-60}"
WAIT_AFTER_SERVER_START="${WAIT_AFTER_SERVER_START:-20}"
# Choose archive format: "tar.bz2" is default; set to "zip" if preferred
DOWNLOAD_FORMAT="${DOWNLOAD_FORMAT:-tar.bz2}"
# Choose which checksum type to use: "sha256" (default), "md5", or "sha512"
CHECKSUM_TYPE="${CHECKSUM_TYPE:-sha256}"

# Default service control commands.
# If you are not using monit, you can override these with your own commands.
STOP_SERVICE_CMD=(monit stop "$WEB_SERVICE")
START_SERVICE_CMD=(monit start "$WEB_SERVICE")

# Check minimum requirements
for cmd in curl wget php mktemp; do
    if ! command -v "$cmd" >/dev/null; then
        echo -e "\033[1;31m\033[1m[x]\033[0m '$cmd' is required but not found"
        exit 1
    fi
done

# function to get config values
# uses `sudo -u www-data php NEXTCLOUD_PATH/occ config:system:get xxx` to get the value for the key xxx
function get_config_value {
    sudo -u www-data php "$NEXTCLOUD_PATH/occ" config:system:get "$1"
}

# function to print a string according to the level
function print_log {
    case "$1" in
    info)
        echo -e "\033[1;36m\033[1m[i]\033[0m $2"
        ;;
    success)
        echo -e "\033[1;32m\033[1m[\u2713]\033[0m $2"
        ;;
    error)
        echo -e "\033[1;31m\033[1m[x]\033[0m $2"
        ;;
    warning)
        echo -e "\033[1;33m\033[1m[!]\033[0m $2"
        ;;
    esac
}

# Function to display a countdown timer
function countdown {
    local seconds=$1
    while [ "$seconds" -gt 0 ]; do
        printf "\r\033[1;36m\033[1m[i]\033[0m Continue in %02d seconds" "$seconds"
        sleep 1
        ((seconds--))
    done
    printf "\r\033[1;36m\033[1m[i]\033[0m Continue in %02d seconds\n" "$seconds"
}

echo -e "\033[1;35m*** Nextcloud Upgrade Script ***\033[0m"

# make sure that the script is run as root
if [ "$EUID" -ne 0 ]; then
    print_log error "Please run as root"
    exit 1
fi

if [ ! -d "$NEXTCLOUD_PATH" ]; then
    print_log error "Nextcloud path '$NEXTCLOUD_PATH' does not exist"
    exit 1
fi

if [ ! -d "$BACKUP_PATH" ]; then
    print_log error "Backup path '$BACKUP_PATH' does not exist"
    exit 1
fi

current_version=$(get_config_value version)

# read the version to upgrade to from the first (optional) argument
if [ -z "$1" ]; then

    print_log info "Checking for updates"

    # Extract version and build information from version.php
    OC_Channel=$(grep 'OC_Channel' "$NEXTCLOUD_PATH/version.php" | awk -F"'" '{print $2}')
    OC_Build=$(grep 'OC_Build' "$NEXTCLOUD_PATH/version.php" | awk -F"'" '{print $2}')

    # Get PHP version components
    PHP_MAJOR_VERSION=$(php -r 'echo PHP_MAJOR_VERSION;')
    PHP_MINOR_VERSION=$(php -r 'echo PHP_MINOR_VERSION;')
    PHP_RELEASE_VERSION=$(php -r 'echo PHP_RELEASE_VERSION;')

    # URL encode the build time
    OC_Build_Encoded=$(php -r "echo urlencode('$OC_Build');")

    # Construct the update URL (we only use it to get the new version)
    updateURL="https://updates.nextcloud.com/updater_server/?version=$(echo "$current_version" | tr '.' 'x')xxx${OC_Channel}xx${OC_Build_Encoded}x${PHP_MAJOR_VERSION}x${PHP_MINOR_VERSION}x${PHP_RELEASE_VERSION}"

    # Make the curl request
    if ! response=$(curl -s -H "User-Agent: Nextcloud Updater" "$updateURL"); then
        print_log error "Could not do request to updater server at '$updateURL'"
        exit 1
    fi

    # Check if response is empty
    if [ -z "$response" ]; then
        print_log warning "No update available at this time"
        exit 0
    fi

    # Extract version from the response
    new_version=$(echo "$response" | sed -n 's:.*<version>\(.*\)</version>.*:\1:p')

else
    new_version="$1"
fi

# Adjust version for download file naming.
# If new_version has four dot-separated parts (e.g., X.Y.Z.a), then drop the fourth part.
download_version=$(echo "$new_version" | awk -F. '{if(NF==4){print $1"."$2"."$3} else {print $0}}')

# Build the download URL using the base URL for both zip and tar.bz2 releases.
download_url="https://download.nextcloud.com/server/releases/nextcloud-${download_version}.${DOWNLOAD_FORMAT}"
# Define the local archive file name
archive_file="nextcloud-${download_version}.${DOWNLOAD_FORMAT}"

# if new_version and current_version are the same, exit
if [ "$new_version" == "$current_version" ]; then
    print_log success "You are already on the latest version"
    exit 0
fi

print_log success "New version '$new_version' available"

# download new version
print_log info "Downloading Nextcloud '$new_version'"
if ! wget "$download_url" -O "$archive_file"; then
    print_log error "Could not download Nextcloud from $download_url"
    exit 1
fi

# Determine the checksum command based on CHECKSUM_TYPE
case "$CHECKSUM_TYPE" in
    sha256)
        checksum_cmd="sha256sum"
        ;;
    md5)
        checksum_cmd="md5sum"
        ;;
    sha512)
        checksum_cmd="sha512sum"
        ;;
    *)
        print_log error "Unsupported checksum type: $CHECKSUM_TYPE"
        exit 1
        ;;
esac

# download checksum file using the selected type (e.g. sha256)
print_log info "Downloading ${CHECKSUM_TYPE} checksum"
if ! wget "$download_url.${CHECKSUM_TYPE}" -O "$archive_file.${CHECKSUM_TYPE}"; then
    print_log error "Could not download ${CHECKSUM_TYPE} checksum from $download_url.${CHECKSUM_TYPE}"
    exit 1
fi

# verify checksum: only check the line corresponding to our archive file
print_log info "Verifying checksum"
if ! grep "$archive_file\$" "$archive_file.${CHECKSUM_TYPE}" | $checksum_cmd -c -; then
    print_log error "Checksum verification failed"
    exit 1
fi

# Display current and new version and ask for confirmation to proceed
print_log info "Current version: $current_version"
print_log info "New version available: $new_version"
read -r -p "Do you want to proceed with the upgrade? [Y/n]: " answer
case "$answer" in
    [Nn]* )
        print_log info "Upgrade aborted by user."
        exit 0
        ;;
    * )
        print_log info "Proceeding with upgrade."
        ;;
esac

# backup database
db_type=$(get_config_value dbtype)
db_name=$(get_config_value dbname)
db_user=$(get_config_value dbuser)
db_password=$(get_config_value dbpassword)
db_host=$(get_config_value dbhost)
utf8mb4=$(get_config_value mysql.utf8mb4)
date=$(date +"%Y%m%d")

# put nextcloud in maintenance mode
sudo -u www-data php "$NEXTCLOUD_PATH/occ" maintenance:mode --on

# wait for connections to settle
countdown "$WAIT_BEFORE_BACKUP"

if [ "$db_type" = "pgsql" ]; then
    # PostgreSQL backup command
    export PGPASSWORD="$db_password"
    if ! pg_dump "$db_name" -h "$db_host" -U "$db_user" -f "$BACKUP_PATH/nextcloud-sqlbkp_${current_version}_${date}.bak"; then
        print_log error "PostgreSQL backup failed."
        unset PGPASSWORD
        exit 1
    fi
    unset PGPASSWORD
elif [ "$db_type" = "mysql" ]; then
    # Create a temporary credentials file
    print_log info "Creating temporary credentials file for database backup"
    tmp_credentials=$(mktemp)
    if [ ! -f "$tmp_credentials" ]; then
        print_log error "Could not create temporary credentials file"
        exit 1
    fi
    chmod 600 "$tmp_credentials"

    # Ensure cleanup on exit or error
    cleanup() {
        rm -f "$tmp_credentials"
    }
    trap cleanup EXIT

cat > "$tmp_credentials" <<EOF
[client]
user=$db_user
password=$db_password
host=$db_host
EOF


    # MySQL/MariaDB backup command
    if [ "$utf8mb4" == "true" ]; then
        mysqldump --defaults-extra-file="$tmp_credentials" --single-transaction --default-character-set=utf8mb4 "$db_name" > "$BACKUP_PATH/nextcloud-sqlbkp_${current_version}_${date}.bak"
    else
        mysqldump --defaults-extra-file="$tmp_credentials" --single-transaction "$db_name" > "$BACKUP_PATH/nextcloud-sqlbkp_${current_version}_${date}.bak"
    fi

else
    print_log error "Unsupported database type: $db_type"
    exit 1
fi

# compress the database backup
print_log info "Compressing database backup"
gzip -f "$BACKUP_PATH/nextcloud-sqlbkp_${current_version}_${date}.bak"

# backup nextcloud config folder
print_log info "Backing up Nextcloud config folder"
cp -r "$NEXTCLOUD_PATH"/config "$BACKUP_PATH/nextcloud-dirbkp_${current_version}_${date}"

temp_extract_dir=$(mktemp -d -t nextcloud_extract_XXXXXX)
print_log info "Extracting Nextcloud to '$temp_extract_dir'"
if [ "$DOWNLOAD_FORMAT" = "zip" ]; then
    unzip -q "$archive_file" -d "$temp_extract_dir"
else
    tar -xjf "$archive_file" -C "$temp_extract_dir"
fi

print_log info "Stopping web server"
if ! "${STOP_SERVICE_CMD[@]}"; then
    print_log error "Failed to stop web server using command: ${STOP_SERVICE_CMD[*]}. Aborting upgrade."
    exit 1
fi

print_log info "Disabling cron job"
crontab -u www-data -l > /tmp/www-data-crontab.bak || true
sed -i '/^[^#]*php -f .*cron.php/ s/^/#/' /tmp/www-data-crontab.bak
crontab -u www-data /tmp/www-data-crontab.bak

if [ -d "${NEXTCLOUD_PATH}.old" ]; then
    print_log warning "Removing old installation dir at '${NEXTCLOUD_PATH}.old'"
    rm -rf "${NEXTCLOUD_PATH}.old"
fi

print_log info "Moving current Nextcloud to '$NEXTCLOUD_PATH.old'"
mv "$NEXTCLOUD_PATH" "${NEXTCLOUD_PATH}.old"

print_log info "Moving new Nextcloud to '$NEXTCLOUD_PATH'"
mv "$temp_extract_dir/nextcloud" "$NEXTCLOUD_PATH"

rm -rf "$temp_extract_dir"

print_log info "Restoring 'config.php'"
cp "${NEXTCLOUD_PATH}.old/config/config.php" "$NEXTCLOUD_PATH/config/config.php"

print_log info "Fixing permissions"
chown -R www-data:www-data "$NEXTCLOUD_PATH"
find "$NEXTCLOUD_PATH"/ -type d -exec chmod 750 {} \;
find "$NEXTCLOUD_PATH"/ -type f -exec chmod 640 {} \;

print_log info "Starting web server"
if ! "${START_SERVICE_CMD[@]}"; then
    print_log error "Failed to start web server using command: ${START_SERVICE_CMD[*]}. Please start the web server manually."
fi

# wait for server to start
countdown "$WAIT_AFTER_SERVER_START"

print_log info "Upgrading Nextcloud"
if ! sudo -u www-data php "$NEXTCLOUD_PATH/occ" upgrade | tee /tmp/nextcloud_upgrade.log; then
    print_log error "Could not upgrade Nextcloud, check /tmp/nextcloud_upgrade.log for more information"
    exit 1
fi

print_log info "Re-enabling cron job"
# Uncomment the cron job line
sed -i '/^#.*php -f .*cron.php/ s/^#//' /tmp/www-data-crontab.bak
# Ensure the cron job exists; if not, append it
if ! grep -q 'php -f .*cron.php' /tmp/www-data-crontab.bak; then
    echo "*/15 * * * * php -f $NEXTCLOUD_PATH/cron.php" >> /tmp/www-data-crontab.bak
fi
crontab -u www-data /tmp/www-data-crontab.bak
# Remove the backup file
rm -f /tmp/www-data-crontab.bak

# take nextcloud out of maintenance mode
sudo -u www-data php "$NEXTCLOUD_PATH/occ" maintenance:mode --off

print_log info "Please check the Nextcloud web interface to make sure everything is working correctly"
print_log success "Upgrade complete"
