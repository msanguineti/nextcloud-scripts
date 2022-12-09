#!/bin/sh

#
# This script upgrades an existing Nextcloud installation to the specified version or, by default, the latest version.
# It will backup both the Nextcloud installation directory and the database, respectively:
# - nextcloud-dirbkp.tar.gz
# - nextcloud-sqlbkp.sql.gz
# in the directory where the script is run.
#

usage() {
  printf "Usage: %s [OPTIONS]... [VERSION]\n\n" "$(basename "$0")"
  printf "Upgrade Nextcloud to the specified version or the latest version.\n\n"
  printf "Options:\n"
  printf "  -i, --install-dir=PATH           Path to Nextcloud install directory (default: /var/www/nextcloud)\n"
  printf "  -u, --web-user=USER              User to run web server as (default: www-data)\n"
  printf "  -s, --web-server=SERVER          Web server to use (default: nginx)\n"
  printf "  -f, --force-download             Force download of Nextcloud\n"
  printf "  --no-cleanup                     Do not perform cleanups after upgrade\n"
  printf "  --no-backup                      Do not backup old Nextcloud installation\n"
  printf "  --debug                          Print variables and exit\n"
  printf "  --dry-run                        Do not run commands, just print what would be run\n"
  printf "  -h, --help                       Display this help and exit\n"
  exit 1
}

colorize() {
  case $1 in
  red)
    printf "\033[0;31m%s\033[0m" "$2"
    ;;
  green)
    printf "\033[0;32m%s\033[0m" "$2"
    ;;
  blue)
    printf "\033[0;34m%s\033[0m" "$2"
    ;;
  yellow)
    printf "\033[0;33m%s\033[0m" "$2"
    ;;
  esac
}

# run a command
# $1 - description of what is being run
# the rest - command to run
run() {
  message=$1
  shift
  if [ "${dry_run}" = true ]; then
    printf "Would run: %s\n" "$(colorize blue "$*")"
  else
    printf "%s... " "$(colorize blue "${message}")"
    if output=$("$@" 2>&1); then
      # print a green checkmark
      printf "%s\n" "$(colorize green "✓")"
    else
      # red x
      printf "%s %s\n" "$(colorize red "✗")" "${output}"
      exit 1
    fi
  fi
}

# check for root
if [ "$(id -u)" -ne 0 ]; then
  printf "This script must be run as root\n"
  exit 1
fi

# check for wget, tar and rsync
for cmd in wget tar rsync bzip2; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    printf "Could not find %s. Please install it and try again.\n" "$(colorize red "${cmd}")"
    exit 1
  fi
done

# set variables
version=latest
path=/var/www/nextcloud
dry_run=false
web_user=www-data
web_server=nginx
no_backup=false
force_download=false
cleanup=true
debug=false

# parse arguments and options
# parse options
while [ $# -gt 0 ]; do
  case $1 in
  -i)
    path=$2
    shift 2
    ;;
  --install-dir=*)
    path=${1#*=}
    shift
    ;;
  --no-backup)
    no_backup=true
    shift
    ;;
  --dry-run)
    dry_run=true
    shift
    ;;
  -u)
    web_user=$2
    shift 2
    ;;
  --web-user=*)
    web_user=${1#*=}
    shift
    ;;
  -s)
    web_server=$2
    shift 2
    ;;
  --web-server=*)
    web_server=${1#*=}
    shift
    ;;
  -f | --force-download)
    force_download=true
    shift
    ;;
  --no-cleanup)
    cleanup=false
    shift
    ;;
  -h | --help)
    usage
    ;;
  -d | --debug)
    debug=true
    shift
    ;;
  -*)
    printf "Unknown option: %s\n\n" "$(colorize red "$1")"
    usage
    ;;
  [0-9]*.[0-9]*.[0-9]*)
    version=nextcloud-$1
    shift
    ;;
  *)
    printf "Unknown argument: %s\n\n" "$(colorize red "$1")"
    usage
    ;;
  esac
done

# print all the variables and exit if debugging
if [ "${debug}" = true ]; then
  printf "Version to upgrade to:        %s\n" "$(colorize blue "${version}")"
  printf "Path to install directory:    %s\n" "$(colorize blue "${path}")"
  printf "User to run web server as:    %s\n" "$(colorize blue "${web_user}")"
  printf "Web server to use:            %s\n" "$(colorize blue "${web_server}")"
  printf "Force download of Nextcloud:  %s\n" "$(colorize blue "${force_download}")"
  printf "Cleanup downloaded files:     %s\n" "$(colorize blue "${cleanup}")"
  exit 0
fi

# check install directory exists and nextcloud is installed
if [ ! -d "${path}" ] || [ ! -f "${path}"/config/config.php ]; then
  printf "Nextcloud is not installed in %s\n" "$(colorize red "${path}")"
  # if not dry run, exit
  if [ "${dry_run}" = false ]; then
    exit 1
  fi
fi

# activate maintenance mode
run "activating maintenance mode" sudo -u "${web_user}" php "${path}"/occ maintenance:mode --on

# sleep for 1 minute to allow clients to sync
run "sleeping for 1 minute" sleep 60

# stop web server
run "stopping web server" service "${web_server}" stop

# if a nextcloud directory exists in CWD, delete it
if [ -d nextcloud ]; then
  run "cleanup working directory" rm -rf nextcloud
fi

# backup old nextcloud
if [ "${no_backup}" = false ]; then
  # create backup directory if it doesn't exist
  if [ ! -d nextcloud-dirbkp ]; then
    run "creating backup directory" mkdir nextcloud-dirbkp
  fi
  run "backing up old nextcloud" rsync --delete -Aax "${path}"/ nextcloud-dirbkp/
fi

# backup database according to which database is used
if [ "${no_backup}" = false ]; then
  if [ "${dry_run}" = true ]; then
    db_type="mysql"
    username="root"
    password="password"
    db_name="nextcloud"
    server="localhost"
    utf8mb4=true
  else
    db_type=$(grep dbtype nextcloud-dirbkp/config/config.php | cut -d "'" -f 4)
    username=$(grep dbuser nextcloud-dirbkp/config/config.php | cut -d "'" -f 4)
    password=$(grep dbpassword nextcloud-dirbkp/config/config.php | cut -d "'" -f 4)
    db_name=$(grep dbname nextcloud-dirbkp/config/config.php | cut -d "'" -f 4)
    server=$(grep dbhost nextcloud-dirbkp/config/config.php | cut -d "'" -f 4)
    utf8mb4=$(grep mysql.utf8mb4 nextcloud-dirbkp/config/config.php | grep -q true)
  fi
  case "${db_type}" in
  mysql)
    # if mysql.utf8mb4 is true, run mysqldump with --default-character-set=utf8mb4
    if ${utf8mb4}; then
      run "backing up database" mysqldump -h "${server}" -u "${username}" -p"${password}" "${db_name}" --default-character-set=utf8mb4 >nextcloud-sqlbkp.bak
    else
      run "backing up database" mysqldump -h "${server}" -u "${username}" -p"${password}" "${db_name}" >nextcloud-sqlbkp.bak
    fi
    ;;
  pgsql)
    run "backing up database" PGPASSWORD="${password}" pg_dump -h "${server}" -U "${username}" -d "${db_name}" >nextcloud-sqlbkp.bak
    ;;
  sqlite)
    run "backing up database" cp "${path}"/data/"${db_name}".db nextcloud-sqlbkp.bak
    ;;
  *)
    printf "Unknown database type: %s\n" "$(colorize red "${db_type}")"
    exit 1
    ;;
  esac
fi

# download nextcloud if it doesn't exist or if force_download is true
if [ ! -f "${version}".tar.bz2 ] || [ ${force_download} = true ]; then
  run "downloading nextcloud" wget https://download.nextcloud.com/server/releases/"${version}".tar.bz2
fi

# download sha256sum even if we downloaded nextcloud before
run "downloading sha256sum" wget https://download.nextcloud.com/server/releases/"${version}".tar.bz2.sha256

# check sha256sum
run "checking sha256sum" sha256sum -c "${version}".tar.bz2.sha256

# extract nextcloud
run "extracting nextcloud" tar -xjf "${version}".tar.bz2

# copy backup config.php to new nextcloud
run "copying backup config.php" rsync -aq nextcloud-dirbkp/config/config.php nextcloud/config/

# copy new nextcloud to path
run "copying new nextcloud" rsync --delete -Aaxq nextcloud/ "${path}"/

# compress backups
if [ "${no_backup}" = false ]; then
  run "compressing backups" tar -cjf nextcloud-dirbkp.tar.bz2 nextcloud-dirbkp && rm -rf nextcloud-dirbkp
  # compress database backup file
  run "compressing database backup" bzip2 nextcloud-sqlbkp.bak
fi

# delete downloaded files and remove local nextcloud directory if cleanup is true
if [ ${cleanup} = true ]; then
  run "deleting downloaded files" rm -f "${version}".tar.bz2 "${version}".tar.bz2.sha256
  run "removing local nextcloud directory" rm -rf nextcloud
fi

# fix permissions
run "fixing permissions" chown -R "${web_user}":"${web_user}" "${path}"/

# fix file/directory modes
run "fixing file mode" find "${path}"/ -type f -exec chmod 0640 {} \;
run "fixing directory mode" find "${path}"/ -type d -exec chmod 0750 {} \;

# start web server
run "starting web server" service "${web_server}" start

# sleep for 20 seconds to allow web server to start
run "sleeping for 20 seconds" sleep 20

# run occ upgrade
run "running occ upgrade" sudo -u "${web_user}" php "${path}"/occ upgrade

# disable maintenance mode
run "disabling maintenance mode" sudo -u "${web_user}" php "${path}"/occ maintenance:mode --off

# exit program
exit 0
