#!/usr/bin/env bash

# ============================================================
#              DOCKER HUB IMAGE PUBLISHER
# ============================================================
# Interactive utility for publishing local Docker images
# to Docker Hub.
#
# First run:
#   bash docker-push.sh
#
# Future runs:
#   ./docker-push.sh
#
# The script automatically makes itself executable.
# ============================================================


# ─────────────────────────────────────────────────────────────
# Make the script executable for future runs
# ─────────────────────────────────────────────────────────────

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"

if [[ -f "$SCRIPT_PATH" && ! -x "$SCRIPT_PATH" ]]; then
    chmod +x "$SCRIPT_PATH" 2>/dev/null || true
fi


set -Eeuo pipefail


# ─────────────────────────────────────────────────────────────
# Colors
# ─────────────────────────────────────────────────────────────

RESET="\033[0m"
BOLD="\033[1m"

CYAN="\033[36m"
BLUE="\033[34m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
WHITE="\033[97m"
GRAY="\033[90m"


# ─────────────────────────────────────────────────────────────
# UI Helpers
# ─────────────────────────────────────────────────────────────

clear_screen() {
    clear 2>/dev/null || printf "\033c"
}

line() {
    printf "${GRAY}────────────────────────────────────────────────────────────${RESET}\n"
}

success() {
    printf "${GREEN}✔ %s${RESET}\n" "$1"
}

warning() {
    printf "${YELLOW}⚠ %s${RESET}\n" "$1"
}

error() {
    printf "${RED}✖ %s${RESET}\n" "$1" >&2
}

info() {
    printf "${CYAN}ℹ %s${RESET}\n" "$1"
}

section() {
    echo
    printf "${BLUE}${BOLD}▶ %s${RESET}\n" "$1"
    line
}

header() {
    clear_screen

    printf "${CYAN}${BOLD}"
    cat <<'EOF'

╔════════════════════════════════════════════════════════════╗
║                                                            ║
║              🐳  DOCKER HUB PUBLISHER                      ║
║                                                            ║
║           Publish local Docker images easily               ║
║                                                            ║
╚════════════════════════════════════════════════════════════╝

EOF
    printf "${RESET}"
}


# ─────────────────────────────────────────────────────────────
# Cleanup / Error Handler
# ─────────────────────────────────────────────────────────────

on_error() {
    local exit_code=$?

    echo
    error "An unexpected error occurred."
    printf "${GRAY}Exit code: %s${RESET}\n" "$exit_code"

    exit "$exit_code"
}

trap on_error ERR


# ─────────────────────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────────────────────

header


# ─────────────────────────────────────────────────────────────
# Step 1 — Check Docker installation
# ─────────────────────────────────────────────────────────────

section "Checking Docker"

if ! command -v docker >/dev/null 2>&1; then
    error "Docker is not installed or is not available in PATH."
    echo
    echo "Install Docker and run this script again."
    exit 1
fi

success "Docker CLI found."

if ! docker info >/dev/null 2>&1; then
    error "Docker is installed, but the Docker daemon is not running."
    echo
    echo "Start Docker Desktop or the Docker service, then try again."
    exit 1
fi

success "Docker daemon is running."


# ─────────────────────────────────────────────────────────────
# Step 2 — Docker Hub Account
# ─────────────────────────────────────────────────────────────

section "Docker Hub Account"

while true; do

    read -rp "Docker Hub username: " DOCKER_USER

    if [[ -n "$DOCKER_USER" ]]; then
        break
    fi

    warning "Username cannot be empty."

done


# Convert username to lowercase
DOCKER_USER="$(echo "$DOCKER_USER" | tr '[:upper:]' '[:lower:]')"


# ─────────────────────────────────────────────────────────────
# Step 3 — Login
# ─────────────────────────────────────────────────────────────

section "Docker Hub Login"

info "Docker will securely request your credentials."
info "Using a Personal Access Token is recommended."
echo

if docker login --username "$DOCKER_USER"; then

    echo
    success "Successfully authenticated as $DOCKER_USER."

else

    echo
    error "Docker Hub login failed."
    exit 1

fi


# ─────────────────────────────────────────────────────────────
# Step 4 — Load local Docker images
# ─────────────────────────────────────────────────────────────

section "Local Docker Images"

IMAGE_LIST=()

while IFS= read -r image; do

    [[ -n "$image" ]] && IMAGE_LIST+=("$image")

done < <(
    docker image ls \
        --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}' \
        | grep -v '^<none>:<none>' || true
)


if [[ ${#IMAGE_LIST[@]} -eq 0 ]]; then
    error "No tagged Docker images were found locally."
    exit 1
fi


printf "\n${BOLD}%-5s %-38s %-16s %-10s${RESET}\n" \
    "#" "IMAGE" "IMAGE ID" "SIZE"

line


for i in "${!IMAGE_LIST[@]}"; do

    IFS='|' read -r IMAGE_NAME IMAGE_ID IMAGE_SIZE <<< "${IMAGE_LIST[$i]}"

    printf "${CYAN}%-5s${RESET} %-38s %-16s %-10s\n" \
        "$((i + 1))" \
        "$IMAGE_NAME" \
        "$IMAGE_ID" \
        "$IMAGE_SIZE"

done


# ─────────────────────────────────────────────────────────────
# Step 5 — Select Images
# ─────────────────────────────────────────────────────────────

echo
printf "${WHITE}${BOLD}Select which image(s) you want to push${RESET}\n"
echo

printf "  ${CYAN}1${RESET}       Push image #1\n"
printf "  ${CYAN}1 3 5${RESET}   Push images #1, #3 and #5\n"
printf "  ${CYAN}all${RESET}     Push all listed images\n"
printf "  ${CYAN}q${RESET}       Quit\n"

echo


while true; do

    read -rp "Selection: " SELECTION

    if [[ "$SELECTION" =~ ^[Qq]$ ]]; then
        echo
        warning "Operation cancelled."
        exit 0
    fi

    if [[ -n "$SELECTION" ]]; then
        break
    fi

    warning "Please select at least one image."

done


declare -a SELECTED_INDEXES=()


if [[ "$SELECTION" =~ ^([Aa][Ll][Ll])$ ]]; then

    for i in "${!IMAGE_LIST[@]}"; do
        SELECTED_INDEXES+=("$i")
    done

else

    read -ra NUMBERS <<< "$SELECTION"

    for NUMBER in "${NUMBERS[@]}"; do

        if ! [[ "$NUMBER" =~ ^[0-9]+$ ]]; then
            error "'$NUMBER' is not a valid image number."
            exit 1
        fi

        INDEX=$((NUMBER - 1))

        if (( INDEX < 0 || INDEX >= ${#IMAGE_LIST[@]} )); then
            error "Image #$NUMBER does not exist."
            exit 1
        fi

        # Prevent duplicate selection
        ALREADY_SELECTED=false

        for EXISTING in "${SELECTED_INDEXES[@]:-}"; do

            if [[ "$EXISTING" == "$INDEX" ]]; then
                ALREADY_SELECTED=true
                break
            fi

        done

        if [[ "$ALREADY_SELECTED" == false ]]; then
            SELECTED_INDEXES+=("$INDEX")
        fi

    done

fi


if [[ ${#SELECTED_INDEXES[@]} -eq 0 ]]; then
    error "No images were selected."
    exit 1
fi


echo
success "${#SELECTED_INDEXES[@]} image(s) selected."


# ─────────────────────────────────────────────────────────────
# Step 6 — Ask if one tag should be used for everything
# ─────────────────────────────────────────────────────────────

section "Tag Configuration"

read -rp "Use the same tag for all selected images? [Y/n]: " SAME_TAG
SAME_TAG="${SAME_TAG:-Y}"

GLOBAL_TAG=""

if [[ "$SAME_TAG" =~ ^[Yy]$ ]]; then

    read -rp "Tag for all images [latest]: " GLOBAL_TAG
    GLOBAL_TAG="${GLOBAL_TAG:-latest}"

fi


# ─────────────────────────────────────────────────────────────
# Step 7 — Configure repositories
# ─────────────────────────────────────────────────────────────

declare -a SOURCE_IMAGES=()
declare -a TARGET_IMAGES=()

section "Configure Docker Hub Destinations"


for INDEX in "${SELECTED_INDEXES[@]}"; do

    IFS='|' read -r LOCAL_IMAGE IMAGE_ID IMAGE_SIZE <<< "${IMAGE_LIST[$INDEX]}"

    SOURCE_IMAGES+=("$LOCAL_IMAGE")

    LOCAL_REPOSITORY="${LOCAL_IMAGE%:*}"
    LOCAL_TAG="${LOCAL_IMAGE##*:}"

    DEFAULT_REPOSITORY="${LOCAL_REPOSITORY##*/}"

    if [[ "$DEFAULT_REPOSITORY" == "<none>" || -z "$DEFAULT_REPOSITORY" ]]; then
        DEFAULT_REPOSITORY="docker-image"
    fi

    echo
    line

    printf "${CYAN}${BOLD}🐳 %s${RESET}\n" "$LOCAL_IMAGE"
    printf "${GRAY}Image ID : %s${RESET}\n" "$IMAGE_ID"
    printf "${GRAY}Size     : %s${RESET}\n" "$IMAGE_SIZE"

    echo


    # Repository
    read -rp "Repository name [$DEFAULT_REPOSITORY]: " REPOSITORY

    REPOSITORY="${REPOSITORY:-$DEFAULT_REPOSITORY}"

    REPOSITORY="$(
        echo "$REPOSITORY" \
        | tr '[:upper:]' '[:lower:]' \
        | tr ' ' '-'
    )"


    # Tag
    if [[ -n "$GLOBAL_TAG" ]]; then

        TAG="$GLOBAL_TAG"

    else

        DEFAULT_TAG="$LOCAL_TAG"

        if [[ "$DEFAULT_TAG" == "<none>" || -z "$DEFAULT_TAG" ]]; then
            DEFAULT_TAG="latest"
        fi

        read -rp "Tag [$DEFAULT_TAG]: " TAG
        TAG="${TAG:-$DEFAULT_TAG}"

    fi


    TARGET="${DOCKER_USER}/${REPOSITORY}:${TAG}"

    TARGET_IMAGES+=("$TARGET")

    echo
    success "Destination configured:"
    printf "   ${GREEN}%s${RESET}\n" "$TARGET"

done


# ─────────────────────────────────────────────────────────────
# Step 8 — Review
# ─────────────────────────────────────────────────────────────

section "Review Before Publishing"

echo

for i in "${!SOURCE_IMAGES[@]}"; do

    printf " ${CYAN}%-35s${RESET}\n" "${SOURCE_IMAGES[$i]}"
    printf "     ↓\n"
    printf " ${GREEN}%s${RESET}\n" "${TARGET_IMAGES[$i]}"

    echo

done


line
echo

printf "${YELLOW}${BOLD}The selected images will now be tagged and uploaded to Docker Hub.${RESET}\n"
echo


read -rp "Continue? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"


if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then

    echo
    warning "Publishing cancelled."
    exit 0

fi


# ─────────────────────────────────────────────────────────────
# Step 9 — Push Images
# ─────────────────────────────────────────────────────────────

declare -a SUCCESSFUL=()
declare -a FAILED=()


for i in "${!SOURCE_IMAGES[@]}"; do

    SOURCE="${SOURCE_IMAGES[$i]}"
    TARGET="${TARGET_IMAGES[$i]}"

    section "Publishing Image $((i + 1)) of ${#SOURCE_IMAGES[@]}"

    printf "Source      : ${WHITE}%s${RESET}\n" "$SOURCE"
    printf "Destination : ${CYAN}%s${RESET}\n" "$TARGET"

    echo


    # Tag
    info "Creating Docker Hub tag..."

    if ! docker tag "$SOURCE" "$TARGET"; then

        error "Failed to tag $SOURCE"
        FAILED+=("$TARGET")
        continue

    fi

    success "Tag created."


    # Push
    echo
    info "Uploading image to Docker Hub..."
    echo

    if docker push "$TARGET"; then

        echo
        success "Upload completed successfully."

        SUCCESSFUL+=("$TARGET")

    else

        echo
        error "Upload failed."

        FAILED+=("$TARGET")

    fi

done


# ─────────────────────────────────────────────────────────────
# Step 10 — Final Summary
# ─────────────────────────────────────────────────────────────

clear_screen


printf "${CYAN}${BOLD}"
cat <<'EOF'

╔════════════════════════════════════════════════════════════╗
║                                                            ║
║                    PUBLISH SUMMARY                         ║
║                                                            ║
╚════════════════════════════════════════════════════════════╝

EOF
printf "${RESET}"


printf "Docker Hub account: ${CYAN}%s${RESET}\n" "$DOCKER_USER"
echo


# Successful images
if [[ ${#SUCCESSFUL[@]} -gt 0 ]]; then

    printf "${GREEN}${BOLD}Successfully Published${RESET}\n"
    line

    for IMAGE in "${SUCCESSFUL[@]}"; do

        printf " ${GREEN}✔${RESET} %s\n" "$IMAGE"

    done

    echo

fi


# Failed images
if [[ ${#FAILED[@]} -gt 0 ]]; then

    printf "${RED}${BOLD}Failed${RESET}\n"
    line

    for IMAGE in "${FAILED[@]}"; do

        printf " ${RED}✖${RESET} %s\n" "$IMAGE"

    done

    echo

fi


line
echo


# ─────────────────────────────────────────────────────────────
# Final Result
# ─────────────────────────────────────────────────────────────

TOTAL="${#SOURCE_IMAGES[@]}"
SUCCESS_COUNT="${#SUCCESSFUL[@]}"
FAILED_COUNT="${#FAILED[@]}"


printf "Total selected : ${WHITE}%s${RESET}\n" "$TOTAL"
printf "Successful     : ${GREEN}%s${RESET}\n" "$SUCCESS_COUNT"
printf "Failed         : ${RED}%s${RESET}\n" "$FAILED_COUNT"

echo


if [[ "$FAILED_COUNT" -eq 0 ]]; then

    printf "${GREEN}${BOLD}"
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║                                                            ║"
    echo "║             🎉 ALL IMAGES PUBLISHED SUCCESSFULLY           ║"
    echo "║                                                            ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    printf "${RESET}"

else

    warning "Some images failed to upload. Review the output above."

fi


echo
printf "${GRAY}Docker Hub:${RESET}\n"
printf "${CYAN}https://hub.docker.com/u/%s${RESET}\n" "$DOCKER_USER"

echo