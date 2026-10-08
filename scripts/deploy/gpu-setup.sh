#!/usr/bin/env bash
# Optional host driver installation. Never executed while collecting wizard answers.
# CUDA runtimes/cuDNN live in the application image, not in host apt packages.
# References and the conservative driver policy are documented in GUIDED_INSTALLATION.md.

gpu_driver_version_ok() {
    python3 - "$1" "$2" <<'PY'
import re,sys
try:
    versions=[]
    for value in sys.argv[1:]:
        assert re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', value)
        parts=tuple(map(int,value.split('.')))
        versions.append(parts + (0,)*(4-len(parts)))
    sys.exit(0 if versions[0]>=versions[1] else 1)
except (ValueError,AssertionError):
    sys.exit(1)
PY
}

gpu_driver_package_valid() {
    [ "$1" = auto ] || [[ "$1" =~ ^nvidia-driver-[0-9]{3}(-server)?(-open)?$ ]]
}

gpu_cuda_contract() {
    local image
    image="$(awk '$1=="FROM" && $2 ~ /^nvidia\/cuda:/ {print $2;exit}' "$ROOT/docker/Dockerfile.gpu")"
    case "$image" in
        nvidia/cuda:12.4.1-*) GPU_CUDA_VERSION=12.4.1; GPU_DRIVER_MIN=550.54.15 ;;
        *) stage_fail "Unreviewed CUDA image: $image. Update the driver compatibility policy before GPU installation." ;;
    esac
    [ "${IS_WSL2:-0}" != 1 ] || GPU_DRIVER_MIN=551.78
    info "Application CUDA $GPU_CUDA_VERSION; installer driver baseline >= $GPU_DRIVER_MIN (real inference still required)."
}

gpu_host_os() { ( . /etc/os-release; printf '%s' "${ID:-unknown}" ); }
gpu_boot_id() { cat /proc/sys/kernel/random/boot_id; }
gpu_driver_version() {
    nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]'
}

gpu_hardware_present() {
    local device vendor class
    for device in /sys/bus/pci/devices/*; do
        [ -r "$device/vendor" ] && [ -r "$device/class" ] || continue
        read -r vendor < "$device/vendor"; read -r class < "$device/class"
        if [ "$vendor" = 0x10de ] && [[ "$class" == 0x03* ]]; then return 0; fi
    done
    return 1
}

gpu_reboot_required() {
    STAGE_RESULT["$CURRENT_STAGE"]=REBOOT_REQUIRED
    STAGE_DETAIL["$CURRENT_STAGE"]='Driver installation needs a reboot; deployment is not complete.'
    printf '\nGPU driver installed or pending activation. Installation is paused before application startup.\n'
    printf '1. Save work and reboot the server yourself when ready. No automatic reboot is performed.\n'
    printf '2. Complete Secure Boot/MOK enrollment if Ubuntu requests it at boot.\n'
    printf '3. Run nvidia-smi, then rerun sudo ./deploy.sh wizard and choose NVIDIA GPU.\n'
    printf 'If the driver still cannot load, resolve kernel-module/Secure Boot errors before retrying.\n'
    print_report
    exit 75
}

gpu_install_ubuntu_driver() {
    [ "$(gpu_host_os)" = ubuntu ] && [ "$(uname -m)" = x86_64 ] || \
        stage_fail 'Automatic kernel-driver installation supports native Ubuntu x86_64 only. Install a supported driver using your OS vendor guide, then rerun in verify mode.'
    gpu_hardware_present || stage_fail 'No NVIDIA display/compute PCI device found. Check GPU installation or VM passthrough before installing a driver.'
    gpu_driver_package_valid "$NVIDIA_DRIVER_PACKAGE" || stage_fail 'Invalid NVIDIA driver package name'
    if [ "$DRY_RUN" = 1 ] || [ "${VALIDATE_ONLY:-0}" = 1 ]; then
        stage_fail 'Preview: a compatible driver must be installed and the host rebooted before remaining GPU checks. No driver/package changes made.'
    fi
    [ "${ONLINE:-0}" = 1 ] || stage_fail 'Offline: supply/install the correct signed driver and kernel headers locally, reboot, then rerun GPU verification.'
    run apt-get update -qq || stage_fail 'Cannot refresh Ubuntu package metadata'
    run apt-get install -y ubuntu-drivers-common pciutils mokutil || stage_fail 'Cannot install Ubuntu GPU discovery tools'
    local available package candidate
    available="$(ubuntu-drivers devices)" || stage_fail 'Ubuntu could not discover supported GPU drivers'
    package="$NVIDIA_DRIVER_PACKAGE"
    if [ "$package" = auto ]; then
        package="$(printf '%s\n' "$available" | awk '/recommended/ {for(i=1;i<=NF;i++) if($i ~ /^nvidia-driver-[0-9]/) print $i}' | sort -u)"
    fi
    gpu_driver_package_valid "$package" && [ "$package" != auto ] || \
        stage_fail 'No single recommended driver package found. Inspect ubuntu-drivers devices and choose a supported --nvidia-driver package explicitly.'
    printf '%s\n' "$available" | awk -v wanted="$package" '{for(i=1;i<=NF;i++) if($i==wanted) found=1} END {exit !found}' || \
        stage_fail "Ubuntu does not list $package as supported for this hardware. Choose a package reported by ubuntu-drivers devices."
    candidate="$(apt-cache policy "$package" | awk '/Candidate:/ {print $2;exit}')"
    candidate="${candidate#*:}"; candidate="${candidate%%-*}"
    gpu_driver_version_ok "$candidate" "$GPU_DRIVER_MIN" || \
        stage_fail "$package candidate ${candidate:-missing} does not meet the CUDA $GPU_CUDA_VERSION policy ($GPU_DRIVER_MIN). Check supported OS repositories/hardware; no driver was installed."
    info "Selected $package ($candidate) from Ubuntu package repositories"
    if have mokutil && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; then
        warn 'Secure Boot is enabled. Follow Ubuntu signing/MOK prompts and enroll the key during reboot; the installer will not disable Secure Boot.'
    fi
    run apt-get install -y "linux-headers-$(uname -r)" "$package" || \
        stage_fail 'Driver installation failed. Check apt output, matching kernel headers and Secure Boot; do not start the application until nvidia-smi works.'
    state_set gpu_driver_pending_boot "$(gpu_boot_id)" || stage_fail "Cannot record pending reboot state"
    state_set gpu_driver_package "$package" || stage_fail "Cannot record installed driver package"
    gpu_reboot_required
}

gpu_docker_runtime_ready() {
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'
}

stage_gpu_host_setup() {
    local may_install="${1:-0}" version pending
    [ "$FORCE_CPU" != 1 ] || { info 'CPU mode: skipping NVIDIA driver/runtime changes'; return 0; }
    version="$(gpu_driver_version)"
    if [ -z "$version" ] && [ "${REQUIRE_GPU:-0}" != 1 ] && [ "$GPU_SETUP_POLICY" != install ]; then
        info 'No active NVIDIA driver; automatic hardware mode will use CPU. Select NVIDIA GPU to require acceleration.'
        return 0
    fi
    gpu_cuda_contract
    pending="$(state_get gpu_driver_pending_boot 2>/dev/null || true)"
    if [ -n "$pending" ] && [ "$pending" = "$(gpu_boot_id)" ]; then gpu_reboot_required; fi
    if ! gpu_driver_version_ok "$version" "$GPU_DRIVER_MIN"; then
        if [ -n "$pending" ]; then
            stage_fail 'Driver remains unavailable/incompatible after reboot. Check nvidia-smi, kernel headers and Secure Boot/MOK enrollment; automatic installation will not loop.'
        fi
        [ "$GPU_SETUP_POLICY" = install ] || \
            stage_fail "Host driver ${version:-missing} does not meet the CUDA $GPU_CUDA_VERSION installer policy (>= $GPU_DRIVER_MIN). Choose GPU setup install/repair or install a compatible driver manually."
        [ "${IS_WSL2:-0}" != 1 ] && ! is_windows_shell || \
            stage_fail 'WSL/Windows: install the NVIDIA driver on Windows and enable Docker Desktop GPU integration; never install a Linux kernel driver inside WSL.'
        gpu_install_ubuntu_driver
        return
    fi
    ok "Keeping compatible NVIDIA driver $version"
    [ -z "$pending" ] || state_set gpu_driver_pending_boot ''
    if have nvidia-ctk && gpu_docker_runtime_ready; then
        ok 'NVIDIA Container Toolkit and Docker NVIDIA runtime are available'
        return 0
    fi
    # Docker Desktop owns its runtime; Linux host reconfiguration is not appropriate there.
    [ "${IS_WSL2:-0}" != 1 ] && ! is_windows_shell || \
        stage_fail 'Enable GPU integration in Docker Desktop, then rerun; host Linux runtime installation is not used on WSL/Windows.'
    [ "$GPU_SETUP_POLICY" = install ] || \
        stage_fail 'NVIDIA Container Toolkit or Docker runtime is missing. Choose GPU setup install/repair, or configure nvidia-ctk runtime configure --runtime=docker manually.'
    if [ "$DRY_RUN" = 1 ] || [ "${VALIDATE_ONLY:-0}" = 1 ]; then
        stage_fail 'Preview: Container Toolkit/runtime installation or configuration is required. No Docker restart or package changes made.'
    fi
    if ! have nvidia-ctk; then
        [ "$may_install" = 1 ] || stage_fail 'Offline: install NVIDIA Container Toolkit packages from approved local media, then rerun.'
        install_nvidia_toolkit_online || stage_fail 'NVIDIA Container Toolkit installation failed'
    fi
    info 'Configuring the Docker NVIDIA runtime; restarting Docker may interrupt other containers.'
    run nvidia-ctk runtime configure --runtime=docker || stage_fail 'Could not configure the Docker NVIDIA runtime'
    run systemctl restart docker || stage_fail 'Docker restart failed; inspect systemctl status docker'
    gpu_docker_runtime_ready || stage_fail 'Docker still does not advertise the NVIDIA runtime'
    ok 'Docker NVIDIA runtime configured; application GPU inference will be tested after startup'
}

wizard_gpu_setup() {
    [ "$FORCE_CPU" != 1 ] || return 0
    printf '\nGPU prerequisite setup (CUDA/cuDNN stay inside the application image)\n'
    printf '  1) Verify existing driver and NVIDIA Container Toolkit (recommended on working servers)\n'
    printf '  2) Install/repair missing prerequisites (Ubuntu driver; supported Toolkit packages)\n'
    printf 'Install/repair may restart Docker and needs a manual reboot after a driver change.\n'
    local default=1
    [ "$GPU_SETUP_POLICY" = install ] && default=2
    while :; do
        wizard_prompt 'GPU setup option' "$default"
        case "$WIZARD_REPLY" in
            1) GPU_SETUP_POLICY=verify; break ;;
            2) GPU_SETUP_POLICY=install; break ;;
            *) printf 'Please enter 1 or 2.\n' ;;
        esac
    done
    if [ "$GPU_SETUP_POLICY" = install ]; then
        while :; do
            wizard_prompt 'Driver package: auto uses Ubuntu hardware recommendation; or nvidia-driver-NNN[-server][-open]' "$NVIDIA_DRIVER_PACKAGE"
            if gpu_driver_package_valid "$WIZARD_REPLY"; then NVIDIA_DRIVER_PACKAGE="$WIZARD_REPLY"; break; fi
            printf 'Enter auto or a specific Ubuntu NVIDIA driver package name.\n'
        done
    fi
}
