#!/usr/bin/env bash
#
# Stage 02 — host prerequisites: Docker Engine, the compose v2 plugin and the
# NVIDIA Container Toolkit.
#
# detect -> validate -> apply -> verify. "Apply" only ever installs userspace
# packages, and only when the host is online and is a real Linux distribution.
#
# The NVIDIA KERNEL DRIVER is deliberately never installed or upgraded here: it
# needs a reboot, it can leave a machine without a display, and the correct
# branch is a site decision. It is detected, and the operator is told exactly
# what to run.

# Minimum compose version: the generated GPU overlay uses the `!override` tag
# to replace (not append to) the base file's device reservations.
COMPOSE_MIN_MAJOR=2
COMPOSE_MIN_MINOR=24

detect_pkg_manager() {
    if have apt-get; then echo apt
    elif have dnf; then echo dnf
    elif have yum; then echo yum
    else echo none; fi
}

compose_version_ok() {
    local raw major minor
    raw="$(docker compose version --short 2>/dev/null)" || return 1
    major="${raw%%.*}"; minor="${raw#*.}"; minor="${minor%%.*}"
    [ -z "${major//[0-9]/}" ] && [ -z "${minor//[0-9]/}" ] || return 1
    [ "$major" -gt "$COMPOSE_MIN_MAJOR" ] && return 0
    [ "$major" -eq "$COMPOSE_MIN_MAJOR" ] && [ "$minor" -ge "$COMPOSE_MIN_MINOR" ]
}

install_docker_online() {
    local mgr; mgr="$(detect_pkg_manager)"
    case "$mgr" in
        apt)
            run bash -c 'export DEBIAN_FRONTEND=noninteractive
                install -m 0755 -d /etc/apt/keyrings
                curl -fsSL https://download.docker.com/linux/$(. /etc/os-release; echo "$ID")/gpg \
                    -o /etc/apt/keyrings/docker.asc && chmod a+r /etc/apt/keyrings/docker.asc
                . /etc/os-release
                echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
                apt-get update -qq
                apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin' ;;
        dnf|yum)
            run bash -c "$mgr config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null || true
                $mgr install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin" ;;
        *)
            return 1 ;;
    esac
    run systemctl enable --now docker 2>/dev/null || true
}

install_nvidia_toolkit_online() {
    local mgr; mgr="$(detect_pkg_manager)"
    case "$mgr" in
        apt)
            run bash -c 'export DEBIAN_FRONTEND=noninteractive
                curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
                curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                  | sed "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" \
                  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
                apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit' ;;
        dnf|yum)
            run bash -c "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
                  -o /etc/yum.repos.d/nvidia-container-toolkit.repo
                $mgr install -y nvidia-container-toolkit" ;;
        *)
            return 1 ;;
    esac
    run nvidia-ctk runtime configure --runtime=docker || return 1
    run systemctl restart docker 2>/dev/null || true
}

stage_sys_install() {
    stage_begin "02 host prerequisites"
    explain "WHAT" "Ensures Docker, the compose v2 plugin and the NVIDIA userspace exist."
    explain "WRITES" "system packages, ONLY on a real Linux host that is online:"
    explain_cont "/etc/apt/keyrings/docker.asc, /etc/apt/sources.list.d/docker.list,"
    explain_cont "nvidia-container-toolkit, then nvidia-ctk runtime configure."
    explain "NEVER" "installs or upgrades the NVIDIA KERNEL DRIVER. It needs a reboot and"
    explain_cont "can leave a machine with no display, so it stays your decision: this"
    explain_cont "stage detects it and prints exactly what to install."
    explain "FAIL" "Docker missing and not installable here, or compose older than 2.24"
    explain_cont "(the generated GPU overlay needs the !override tag)."

    # ---- detect -----------------------------------------------------------
    local docker_present=0 docker_running=0 compose_present=0
    have docker && docker_present=1
    [ "$docker_present" = 1 ] && docker info >/dev/null 2>&1 && docker_running=1
    [ "$docker_running" = 1 ] && docker compose version >/dev/null 2>&1 && compose_present=1

    local driver_present=0 driver_version="" toolkit_present=0
    if have nvidia-smi && nvidia-smi >/dev/null 2>&1; then
        driver_present=1
        driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
    fi
    have nvidia-ctk && toolkit_present=1

    info "docker: $([ "$docker_running" = 1 ] && echo running || echo 'not running')"
    info "compose plugin: $([ "$compose_present" = 1 ] && docker compose version --short 2>/dev/null || echo missing)"
    info "nvidia driver: $([ "$driver_present" = 1 ] && echo "$driver_version" || echo 'not present')"
    info "nvidia container toolkit: $([ "$toolkit_present" = 1 ] && echo present || echo 'not present')"

    # ---- apply (only when online, on Linux, and not validating) -----------
    local may_install=1
    [ "${VALIDATE_ONLY:-0}" = "1" ] && may_install=0
    [ "$DRY_RUN" = "1" ] && may_install=0
    [ "$ONLINE" = "1" ] || may_install=0
    [ "$IS_WSL2" = "1" ] && may_install=0     # Docker Desktop owns the engine
    is_windows_shell && may_install=0

    if [ "$docker_running" != 1 ]; then
        if [ "$IS_WSL2" = "1" ] || is_windows_shell; then
            stage_fail "Docker is not available. Start Docker Desktop (and, under WSL2, enable integration for this distribution), then re-run. Production targets should run Docker Engine on Linux directly."
        elif [ "$may_install" = 1 ]; then
            info "installing Docker Engine + compose plugin"
            install_docker_online || stage_fail "automatic Docker installation is not supported for this distribution — install Docker Engine 24+ and the compose v2 plugin, then re-run"
            docker info >/dev/null 2>&1 || stage_fail "Docker installed but the daemon is not running (systemctl start docker)"
            docker_running=1; compose_present=1
        else
            stage_fail "Docker is not running and cannot be installed here ($([ "$ONLINE" = 1 ] && echo 'validate/dry-run mode' || echo offline)). Install Docker Engine 24+ and the compose v2 plugin, then re-run."
        fi
    fi

    if [ "$compose_present" != 1 ]; then
        stage_fail "the docker compose v2 plugin is missing (docker-compose-plugin)"
    fi
    if ! compose_version_ok; then
        stage_fail "docker compose $(docker compose version --short 2>/dev/null) is older than ${COMPOSE_MIN_MAJOR}.${COMPOSE_MIN_MINOR}, which the generated GPU overlay needs (!override support)"
    fi

    # ---- GPU userspace ----------------------------------------------------
    if [ "$FORCE_CPU" = "1" ]; then
        info "--cpu: GPU prerequisites not required"
    elif [ "$driver_present" = 1 ] && [ "$toolkit_present" != 1 ]; then
        if [ "$may_install" = 1 ]; then
            info "installing the NVIDIA Container Toolkit (userspace only — the kernel driver is never touched)"
            install_nvidia_toolkit_online || stage_fail "NVIDIA Container Toolkit installation failed — install nvidia-container-toolkit and run 'nvidia-ctk runtime configure --runtime=docker'"
            toolkit_present=1
        else
            stage_warn "NVIDIA driver present but the container toolkit is missing — GPU mode is unavailable until 'nvidia-container-toolkit' is installed"
        fi
    elif [ "$driver_present" != 1 ]; then
        # Deliberate: never install or upgrade the kernel driver.
        info "no NVIDIA driver detected — CPU deployment. To enable GPU: install the driver for your card (>= 525), reboot, then re-run 'sudo ./deploy.sh gpu-test'."
    fi

    # ---- verify -----------------------------------------------------------
    docker info >/dev/null 2>&1 || stage_fail "docker daemon is not reachable"
    local summary
    summary="docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), compose $(docker compose version --short 2>/dev/null)"
    [ "$driver_present" = 1 ] && summary="$summary, driver $driver_version"
    [ "$toolkit_present" = 1 ] && summary="$summary, container toolkit"
    stage_pass "$summary"
}
