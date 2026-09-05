#!/bin/bash
# Enhanced update script with minimal output

# Simple color palette
GREEN='\033[0;32m'
RED='\033[1;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Print a header
print_header() {
    printf "\n${BOLD}-------------------------------------------------------${NC}\n"
    printf "${BOLD} %s ${NC}\n" "$1"
    printf "${BOLD}-------------------------------------------------------${NC}\n"
}

# Main title
printf "\n${BOLD}=======================================================${NC}\n"
printf "${BOLD}             CORE SYSTEM UPDATER                  ${NC}\n"
printf "${BOLD}=======================================================${NC}\n\n"

print_header "CHECKING SYSTEM"

printf ">> Checking source directories...\n"
if [ ! -d "/home/dwemer/dwemerdistro/bin" ]; then
    printf "${RED}[ERROR] Source directory /home/dwemer/dwemerdistro/bin not found!${NC}\n"
    exit 1
fi
if [ ! -d "/home/dwemer/dwemerdistro/etc" ]; then
    printf "${RED}[ERROR] Source directory /home/dwemer/dwemerdistro/etc not found!${NC}\n"
    exit 1
fi

printf ">> Checking Git credential helper configuration...\n"
DWEMER_GIT_CONFIG="/home/dwemer/.gitconfig"
if [ -f "$DWEMER_GIT_CONFIG" ] && ! command -v git-credential-manager >/dev/null 2>&1; then
    if git config --file "$DWEMER_GIT_CONFIG" --get-all credential.helper 2>/dev/null | grep -Fxq "manager"; then
        if ! git config --file "$DWEMER_GIT_CONFIG" --unset-all credential.helper '^manager$'; then
            printf "${RED}[ERROR] Could not remove the unavailable Git credential helper.${NC}\n"
            exit 1
        fi
        chown dwemer:dwemer "$DWEMER_GIT_CONFIG" || exit 1
        printf "${GREEN}[SUCCESS] Removed unavailable Git credential helper.${NC}\n"
    fi
fi

print_header "UPDATING SYSTEM FILES"

printf ">> Copying scripts to /usr/local/bin...\n"
cp /home/dwemer/dwemerdistro/bin/* /usr/local/bin/ 2>/dev/null
if [ $? -eq 0 ]; then
    # Set executable permissions for all scripts
    chmod +x /usr/local/bin/* 2>/dev/null
    printf "${GREEN}[SUCCESS] Scripts successfully copied to /usr/local/bin/${NC}\n"
else
    printf "${RED}[ERROR] Error copying scripts to /usr/local/bin/${NC}\n"
    exit 1
fi

# Repair old image keyrings before any subsequent component installation uses APT.
/usr/local/bin/repair_github_cli_keyring || exit 1

printf ">> Checking installed server repository origins...\n"
/usr/local/bin/ddistro_server migrate-remotes all || exit 1

printf ">> Copying configuration files to /etc...\n"
find /home/dwemer/dwemerdistro/etc/ -type f ! -name "php.ini" -exec cp {} /etc/ \; 2>/dev/null
if [ $? -eq 0 ]; then
    printf "${GREEN}[SUCCESS] Configuration files successfully copied to /etc/${NC}\n"
else
    printf "${RED}[ERROR] Error copying configuration files to /etc/${NC}\n"
    exit 1
fi

print_header "UPDATING PHP CONFIGURATION"

# Only a freshly prepared image has the marker that enables this initialization.
/usr/local/bin/initialize_fresh_distro || exit 1

printf ">> Updating PHP Apache configuration...\n"
cp /home/dwemer/dwemerdistro/etc/php.ini /etc/php/8.2/apache2/php.ini 2>/dev/null
if [ $? -eq 0 ]; then
    printf "${GREEN}[SUCCESS] PHP configuration updated${NC}\n"
else
    printf "${RED}[ERROR] Error updating PHP configuration${NC}\n"
    exit 1
fi

printf "\n${BOLD}=======================================================${NC}\n"
printf "${BOLD}${GREEN}        CORE SYSTEM UPDATED SUCCESSFULLY!          ${NC}\n"
printf "${BOLD}=======================================================${NC}\n\n"

#DO NOT REMOVE Begin CHIM Server Update as this triggers the update_gws step in the CHIM launcher.
echo "Begin CHIM Server Update"
