# Nextcloud Upgrade Script

This open source shell script automates the upgrade process for Nextcloud installations. It leverages a technique—observed in Nextcloud’s own GitHub scripts—that queries an updater server to determine the correct upgrade path and download the appropriate upgrade file. The remainder of the upgrade procedure follows the steps suggested in the official Nextcloud documentation.

> **Disclaimer:**  
> This script is provided "as-is", without any warranty. It is not officially endorsed or supported by Nextcloud. Use it at your own risk and always test in a safe environment before deploying in production.

---

## Overview

When you run this script, it will:

1. **Check for Updates:**  
   Query Nextcloud’s updater server (the same method used internally when you click the "Check update" button) to determine if a new version is available and whether your current version is eligible for upgrade.  
   *Note:* Nextcloud’s release files follow a naming convention (e.g., a returned version `30.0.6.2` is adjusted to `30.0.6` for the download).

2. **Download and Verify the Upgrade File:**  
   It constructs the download URL based on your chosen archive format (default is `tar.bz2`, but `zip` is supported) and checksum type (default is SHA256). It then downloads both the archive and its checksum file, filtering the checksum file to verify only the correct line.

3. **Backup Procedures:**  
   - **Database Backup:** Supports both MySQL/MariaDB and PostgreSQL.  
     - For MySQL/MariaDB, it uses a temporary credentials file to securely pass credentials to `mysqldump`.  
     - For PostgreSQL, it sets the `PGPASSWORD` environment variable before running `pg_dump`.
   - **Configuration Backup:** The Nextcloud configuration folder is copied to a backup location.
  
> **Note:** The script does not back up your data directory. It is recommended to back up your data separately.

4. **Upgrade Execution:**  
   - The script puts Nextcloud into maintenance mode.
   - It stops the web service and disables cron jobs.
   - It extracts the downloaded archive, moves the current installation to an "old" directory, installs the new version, restores the configuration file, and fixes file permissions.
   - It then restarts the web service, re-enables cron jobs, and removes maintenance mode.
   
5. **User Confirmation:**  
   Before proceeding, the script displays the current version and the new version and asks for your confirmation.

---

## Features

- **Automated Update Check:** Uses the updater server to automatically determine if an upgrade is available and enforces proper upgrade paths.
- **Flexible Archive Format:** Supports both `tar.bz2` (default) and `zip` files.
- **Checksum Verification:** Downloads and verifies a checksum file (SHA256, MD5, or SHA512) to ensure file integrity.
- **Database Backup:** Supports both MySQL/MariaDB (using a temporary credentials file) and PostgreSQL (using the `PGPASSWORD` environment variable).
- **Customizable Service Control:**  
  The script uses default commands (via `monit`) to stop and start the web server, but these can be easily overridden.
- **Configurable Delays:** Custom delays can be set before backing up and after restarting services.
- **Secure Temporary File Handling:** Uses traps to clean up temporary files if the script exits unexpectedly.
- **Cron Job Management:** Disables and then re-enables cron jobs for the `www-data` user during the upgrade.

---

## Requirements

- **Operating System:** Linux with Bash.
- **Essential Tools:**  
  - `curl`, `wget`
  - `tar` (or `unzip` if using ZIP format)
  - `mysqldump` or `pg_dump`
  - `monit` (by default for service control; if you use another system such as systemd, see configuration below)
  - `php` (for Nextcloud’s `occ` command)
  - `mktemp`
  - Checksum utilities: `sha256sum` (default), `md5sum`, or `sha512sum`
- **Nextcloud Installation:**  
  The script assumes Nextcloud is installed (default path: `/var/www/nextcloud`).

---

## Configuration

The following environment variables can be overridden to suit your environment:

- **NEXTCLOUD_PATH**  
  _Default:_ `/var/www/nextcloud`  
  **Description:** Path to your Nextcloud installation.

- **BACKUP_PATH**  
  _Default:_ `/var/backups/nextcloud`  
  **Description:** Directory where backups are stored. **(Must be an absolute path.)**

- **WEB_SERVICE**  
  _Default:_ `nginx`  
  **Description:** Name of the web server service to be stopped/started.

- **STOP_SERVICE_CMD / START_SERVICE_CMD**  
  _Default:_  
  ```bash
  STOP_SERVICE_CMD=(monit stop "$WEB_SERVICE")
  START_SERVICE_CMD=(monit start "$WEB_SERVICE")
  ```  
  **Description:** Array commands to stop and start your web server.  
  **Example:** If you use systemd, you could override them with:  
  ```bash
  export STOP_SERVICE_CMD=(systemctl stop apache2)
  export START_SERVICE_CMD=(systemctl start apache2)
  ```

- **WAIT_BEFORE_BACKUP**  
  _Default:_ `60`  
  **Description:** Seconds to wait after enabling maintenance mode before starting the backup.

- **WAIT_AFTER_SERVER_START**  
  _Default:_ `20`  
  **Description:** Seconds to wait after starting the web server before proceeding with the upgrade.

- **DOWNLOAD_FORMAT**  
  _Default:_ `tar.bz2`  
  **Description:** Format to download the Nextcloud release (`tar.bz2` or `zip`).

- **CHECKSUM_TYPE**  
  _Default:_ `sha256`  
  **Description:** Checksum algorithm to verify the downloaded archive. Options: `sha256`, `md5`, or `sha512`.

---

## Usage

1. **Install Dependencies:**  
   Ensure all required commands (listed above) are installed.

2. **Download the Script:**  
   Clone or download this repository to your server.

3. **Run the Script as Root:**  
   ```bash
   sudo ./nextcloud-upgrade.sh
   ```

4. **Follow the Prompts:**  
   - The script will check for available updates.
   - It downloads and verifies the upgrade file.
   - It displays your current version and the new version, then asks for confirmation:
     ```
     Do you want to proceed with the upgrade? [Y/n]:
     ```
   - Enter `Y` to continue or `n` to abort.

---

## Database Backup

The script supports both MySQL/MariaDB and PostgreSQL:

- **MySQL/MariaDB:**  
  Uses a temporary credentials file to pass database credentials to `mysqldump` securely.  

- **PostgreSQL:**  
  Uses the `PGPASSWORD` environment variable for authentication and calls `pg_dump` to create the backup.

The database type is determined by reading the `dbtype` value from Nextcloud’s configuration. The backup file is created with a `.bak` extension and then compressed with `gzip`.

---

## Attribution and Explanation

- **Updater Server Technique:**  
  Although the official Nextcloud documentation does not explicitly recommend using the updater server, this script employs a technique found in Nextcloud’s GitHub scripts. This is how the Nextcloud installation determines if an upgrade is available and whether you’re on the correct minor version for a major upgrade. This script automates that process.

- **Upgrade Steps:**  
  The remaining steps—backing up the database and configuration, putting Nextcloud into maintenance mode, stopping the web service, extracting the new version, and restoring permissions—are based on the procedures suggested by the Nextcloud maintainers.

---

## Contributing

Contributions, bug reports, and feature requests are welcome. If you have suggestions or improvements, please open an issue or submit a pull request.

---

## License

This project is licensed under the **MIT License**. See the [LICENSE.md](LICENSE.md) file for details.

---

*Please review and test the script thoroughly before using it in a production environment.*
