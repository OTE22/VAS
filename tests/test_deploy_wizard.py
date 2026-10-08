"""Installer regressions using temporary files and fake Docker calls only.
Run: python3 -m unittest discover -s tests -p test_deploy_wizard.py -v
"""
import os
import pty
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class DeployWizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vas-installer-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(REPO / 'scripts/deploy', self.root / 'scripts/deploy',
                        ignore=shutil.ignore_patterns('__pycache__'))
        source = (REPO / 'deploy.sh').read_text()
        (self.root / 'deploy.sh').write_text(source)
        (self.root / 'library.sh').write_text(source.split('# --self-test runs before')[0])
        (self.root / 'docker').mkdir()
        # Entry-point tests must never invoke real sudo, including on non-root CI.
        self.bin = self.root / 'test-bin'
        self.bin.mkdir()
        fake_id = self.bin / 'id'
        fake_id.write_text('#!/bin/sh\necho 0\n')
        fake_id.chmod(0o755)
        self.entry_env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}
        self.log = self.root / 'docker.calls'

    def run_shell(self, script, *, inputs='', extra=None):
        env = {**os.environ, 'ROOT_FIXTURE': str(self.root), 'CALL_LOG': str(self.log)}
        env.pop('DEPLOY_PACKAGE', None)
        env.update(extra or {})
        return subprocess.run(['bash', '-c', 'source "$ROOT_FIXTURE/library.sh"\n' + script],
                              input=inputs, text=True, capture_output=True, env=env,
                              timeout=20)

    def test_origin_accepts_server_ip_and_dns(self):
        for value, expected in [('10.21.5.22', 'https://10.21.5.22'),
                                ('https://vas.example:443/', 'https://vas.example')]:
            with self.subTest(value=value):
                result = self.run_shell('wizard_origin "$VALUE"', extra={'VALUE': value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_origin_rejects_unsupported_or_unsafe_input(self):
        for value in ['http://vas.example', 'https://vas.example:5555',
                      'https://user:password@vas.example', 'https://vas.example/admin',
                      'https://vas.example/?x=y', 'https://vas.example/#x',
                      'https://999.10.1.1', 'https://[::1]', 'https://x\nENV=bad',
                      'https://$(touch hacked)', 'https://-bad.example', 'https://']:
            with self.subTest(value=value):
                self.assertNotEqual(self.run_shell('wizard_origin "$VALUE"',
                                    extra={'VALUE': value}).returncode, 0)

    def test_load_archive_is_literal_and_only_loaded_once(self):
        archive = self.root / "images ' $(touch injected).tar"
        archive.touch()
        result = self.run_shell('''
            docker() { printf '%s\n' "$@" >> "$CALL_LOG"; }
            IMAGE_ARCHIVES=("$ARCHIVE")
            load_deployment_images && load_deployment_images
        ''', extra={'ARCHIVE': str(archive)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ['load', '--input', str(archive)])
        self.assertFalse((self.root / 'injected').exists())

    def test_package_loads_compressed_and_plain_archives(self):
        images = self.root / 'release/images'
        images.mkdir(parents=True)
        for name in ('one.tar', 'two.tar.gz', 'three.tgz'):
            (images / name).touch()
        result = self.run_shell('''
            docker() { printf '%s\n' "$@" >> "$CALL_LOG"; }
            DEPLOY_PACKAGE="$ROOT/release"
            load_deployment_images
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines().count('load'), 3)

    def test_failed_archive_stops_inventory(self):
        archive = self.root / 'broken.tar'; archive.touch()
        result = self.run_shell('''
            docker() { return 1; }
            compose() { touch "$ROOT/should-not-run"; }
            IMAGE_ARCHIVES=("$ARCHIVE")
            stage_prebuilt_images
        ''', extra={'ARCHIVE': str(archive)})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'should-not-run').exists())

    def test_all_missing_images_reported_before_start(self):
        result = self.run_shell('''
            compose() { printf 'missing-api:release\nmissing-db:release\n'; }
            docker() { return 1; }
            stage_prebuilt_images
            touch "$ROOT/should-not-start"
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing image: missing-api:release', result.stderr)
        self.assertIn('Missing image: missing-db:release', result.stderr)
        self.assertFalse((self.root / 'should-not-start').exists())

    def test_present_images_skip_build_and_pass(self):
        result = self.run_shell('''
            IMAGE_MODE=load
            compose() { printf 'api:release\ndb:release\napi:release\n'; }
            docker() { printf '%s\n' "$*" >> "$CALL_LOG"; }
            compose_mutate() { echo 'UNEXPECTED BUILD'; return 1; }
            stage_build
        ''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('UNEXPECTED BUILD', result.stdout)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_load_dry_run_does_not_call_docker(self):
        archive = self.root / 'saved.tar'; archive.touch()
        result = self.run_shell('''
            DRY_RUN=1
            IMAGE_ARCHIVES=("$ARCHIVE")
            docker() { touch "$ROOT/should-not-run"; }
            compose() { touch "$ROOT/should-not-run"; }
            stage_prebuilt_images
        ''', extra={'ARCHIVE': str(archive)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('have not been validated', result.stdout)
        self.assertFalse((self.root / 'should-not-run').exists())

    def test_compose_load_disallows_build_and_pull(self):
        result = self.run_shell('''
            docker() { printf '%s\n' "$@" >> "$CALL_LOG"; }
            IMAGE_MODE=load
            compose_mutate up -d postgres
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines()[-6:],
                         ['up', '--no-build', '--pull', 'never', '-d', 'postgres'])

    def test_build_mode_preserves_existing_startup(self):
        result = self.run_shell('''
            docker() { printf '%s\n' "$@" >> "$CALL_LOG"; }
            IMAGE_MODE=build
            compose_mutate up -d
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.log.read_text().splitlines()
        self.assertEqual(args[-2:], ['up', '-d'])
        self.assertNotIn('--no-build', args)

    def test_fresh_guide_collects_config_without_writes(self):
        result = self.run_shell('''
            wizard_collect
            printf 'RESULT:%s|%s|%s|%s|%s' "$IMAGE_MODE" "$PUBLIC_ORIGIN_FLAG" "$FORCE_CPU" "$FORCE_OFFLINE" "$DRY_RUN"
        ''', inputs='2\n\n10.21.5.22\n\n2\n1\n\n\n\n2\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RESULT:build|https://10.21.5.22|1|0|1', result.stdout)
        self.assertFalse((self.root / 'docker/.env').exists())
        self.assertFalse((self.root / '.deployment').exists())

    def test_load_guide_retains_archive_and_offline_choice(self):
        archive = self.root / 'saved images.tar'; archive.touch()
        result = self.run_shell('''
            wizard_collect
            printf 'RESULT:%s|%s|%s|%s' "$IMAGE_MODE" "$FORCE_OFFLINE" "$DRY_RUN" "${IMAGE_ARCHIVES[0]}"
        ''', inputs=f'1\n\n{archive}\n\n10.21.5.22\nvas.example\n1\n\n2\n\n\n\n1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'RESULT:load|1|0|{archive}', result.stdout)

    def test_existing_origin_is_preserved(self):
        (self.root / 'docker/.env').write_text('PUBLIC_ORIGIN=https://existing.example\n')
        result = self.run_shell('wizard_collect',
                               inputs='2\n\n10.21.5.22\nnew.example\n\n1\n\n1\n\n\n\n2\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Browser: https://existing.example', result.stdout)
        self.assertEqual((self.root / 'docker/.env').read_text(),
                         'PUBLIC_ORIGIN=https://existing.example\n')

    def test_cancel_never_reaches_deployment(self):
        result = self.run_shell('wizard_collect; touch "$ROOT/should-not-run"',
                               inputs='2\n\n10.21.5.22\nvas.example\n1\n\n1\n\n\n\n3\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Cancelled', result.stdout)
        self.assertFalse((self.root / 'should-not-run').exists())

    def test_eof_cancels_without_guessing_answers(self):
        result = self.run_shell('wizard_collect')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Input closed', result.stderr)

    def test_default_terminal_opens_guide_and_cancel_is_inert(self):
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(['bash', str(self.root / 'deploy.sh')],
                                       stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=self.entry_env)
            os.write(master, b'2\n\n10.21.5.22\nvas.example\n1\n\n1\n\n\n\n3\n')
            output, errors = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, errors)
            self.assertIn('VAS guided installation', output)
            self.assertIn('Cancelled', output)
            self.assertFalse((self.root / '.deployment').exists())
            self.assertFalse((self.root / 'logs').exists())
        finally:
            os.close(master)
            os.close(slave)

    def test_no_arguments_without_terminal_stops_before_writes(self):
        result = subprocess.run(['bash', str(self.root / 'deploy.sh')],
                                input='', text=True, capture_output=True, timeout=10,
                                env=self.entry_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires a terminal', result.stderr)
        self.assertFalse((self.root / '.deployment').exists())
        self.assertFalse((self.root / 'logs').exists())

    def test_guided_privilege_handoff_preserves_literal_arguments(self):
        fake_sudo = self.bin / 'sudo'
        fake_sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALL_LOG"\nexit 23\n')
        fake_sudo.chmod(0o755)
        result = self.run_shell('''
            id() { echo 1000; }
            is_windows_shell() { return 1; }
            is_wsl2() { return 1; }
            wizard_require_privileges wizard '--deploy-package=/tmp/saved images $(touch injected)'
            touch "$ROOT/should-not-run"
        ''', extra={'PATH': str(self.bin) + os.pathsep + os.environ['PATH']})
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(),
                         ['--', 'bash', str(self.root / 'deploy.sh'), 'wizard',
                          '--deploy-package=/tmp/saved images $(touch injected)'])
        self.assertFalse((self.root / 'injected').exists())
        self.assertFalse((self.root / 'should-not-run').exists())

    def test_guided_root_and_preview_do_not_request_sudo(self):
        for setup in ['id() { echo 0; }', 'DRY_RUN=1; id() { echo 1000; }']:
            with self.subTest(setup=setup):
                result = self.run_shell(setup + '''
                    have() { echo 'UNEXPECTED privilege request'; return 1; }
                    wizard_require_privileges
                    echo READY
                ''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'READY')

    def test_guided_missing_sudo_stops_before_questions(self):
        result = self.run_shell('''
            id() { echo 1000; }
            is_windows_shell() { return 1; }
            is_wsl2() { return 1; }
            have() { return 1; }
            wizard_require_privileges
            touch "$ROOT/should-not-run"
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Administrator access is required', result.stderr)
        self.assertFalse((self.root / 'should-not-run').exists())

    def test_guided_docker_desktop_keeps_existing_privilege_policy(self):
        result = self.run_shell('''
            id() { echo 1000; }
            is_windows_shell() { return 1; }
            is_wsl2() { return 0; }
            require_root() { echo 'DESKTOP CHECK'; }
            wizard_require_privileges
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'DESKTOP CHECK')

    def test_load_mode_does_not_require_buildx(self):
        result = self.run_shell('''
            IMAGE_MODE=load; FORCE_CPU=1; ONLINE=0; IS_WSL2=0
            have() { [ "$1" = docker ]; }
            docker() {
                case "$*" in
                    'compose version --short') echo 2.24.4 ;;
                    buildx*) touch "$ROOT/buildx-probed"; return 1 ;;
                esac
                return 0
            }
            stage_sys_install
        ''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / 'buildx-probed').exists())

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for certificate regression')
    def test_ip_certificate_is_valid_and_never_overwritten(self):
        tls_dir = self.root / 'scripts/tls'
        tls_dir.mkdir()
        shutil.copy(REPO / 'scripts/tls/make-internal-ca.sh', tls_dir)
        result = self.run_shell('PUBLIC_ORIGIN_FLAG=https://10.21.5.22; stage_tls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cert = self.root / 'certs/server.crt'
        original = cert.read_bytes()
        checked = subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout',
                                  '-checkip', '10.21.5.22'], capture_output=True)
        self.assertEqual(checked.returncode, 0)
        result = self.run_shell('PUBLIC_ORIGIN_FLAG=https://10.21.5.2; stage_tls')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('certificate does not cover', result.stderr)
        self.assertEqual(cert.read_bytes(), original)

    def test_default_settings_are_only_written_when_applied(self):
        result = self.run_shell('''
            wizard_default_settings
            test ! -f "$ROOT/docker/.env" || exit 20
            wizard_apply_defaults
        ''', inputs='\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = (self.root / 'docker/.env').read_text()
        for entry in ['LOG_LEVEL=INFO', 'DATA_RETENTION_DAYS=365',
                      'BACKUP_RETENTION_DAYS=14', 'MAX_STORAGE_GB=5000']:
            self.assertIn(entry, saved)

    def test_existing_defaults_and_unrelated_secrets_are_preserved(self):
        original = 'LOG_LEVEL=WARNING\nDATA_RETENTION_DAYS=730\nBACKUP_RETENTION_DAYS=30\nMAX_STORAGE_GB=1200\nPRIVATE_KEY=fixture-secret\n'
        (self.root / 'docker/.env').write_text(original)
        result = self.run_shell('wizard_default_settings; wizard_apply_defaults', inputs='\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'docker/.env').read_text(), original)
        self.assertNotIn('fixture-secret', result.stdout + result.stderr)

    def test_custom_defaults_validate_then_apply(self):
        result = self.run_shell('wizard_default_settings; wizard_apply_defaults',
                               inputs='2\nDEBUG\nERROR\n0\n90\n-1\n30\nhello\n2500\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = (self.root / 'docker/.env').read_text()
        for entry in ['LOG_LEVEL=ERROR', 'DATA_RETENTION_DAYS=90',
                      'BACKUP_RETENTION_DAYS=30', 'MAX_STORAGE_GB=2500']:
            self.assertIn(entry, saved)

    def test_default_settings_dry_run_is_inert_but_exports_for_compose(self):
        result = self.run_shell('''
            DRY_RUN=1
            wizard_default_settings
            wizard_apply_defaults
            printf 'RENDER:%s|%s' "$DATA_RETENTION_DAYS" "$MAX_STORAGE_GB"
        ''', inputs='\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RENDER:365|5000', result.stdout)
        self.assertFalse((self.root / 'docker/.env').exists())

    def test_server_ip_validation(self):
        for value in ('10.21.5.22', '192.168.1.50'):
            self.assertEqual(self.run_shell('valid_server_ip "$IP"', extra={'IP': value}).returncode, 0)
        for value in ('', '0.0.0.0', '127.0.0.1', '224.1.1.1', '169.254.1.1', '999.1.1.1', 'vas.example', '10.2.3.4\nBAD=1'):
            self.assertNotEqual(self.run_shell('valid_server_ip "$IP"', extra={'IP': value}).returncode, 0)

    def test_quoted_network_values_keep_their_meaning(self):
        (self.root / 'docker/.env').write_text('PUBLIC_ORIGIN="https://vas.example"\nSERVER_IP="10.21.5.22"\n')
        result = self.run_shell('deployment_browser_origins')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'https://vas.example,https://10.21.5.22')

    def test_network_merges_existing_origins_and_saves_ip(self):
        (self.root / 'docker/.env').write_text('PUBLIC_ORIGIN=https://vas.example\nPUBLIC_ORIGINS=https://other.example,https://vas.example\nPRIVATE_KEY=fixture-secret\n')
        result = self.run_shell('SERVER_IP_FLAG=10.21.5.22; deployment_apply_network')
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = (self.root / 'docker/.env').read_text()
        self.assertIn('PUBLIC_ORIGINS=https://vas.example,https://10.21.5.22,https://other.example', saved)
        self.assertIn('SERVER_IP=10.21.5.22', saved)
        self.assertIn('PRIVATE_KEY=fixture-secret', saved)
        self.assertNotIn('fixture-secret', result.stdout + result.stderr)

    def test_network_dry_run_and_wildcard_rejection(self):
        result = self.run_shell('DRY_RUN=1; PUBLIC_ORIGIN_FLAG=https://vas.example; SERVER_IP_FLAG=10.21.5.22; deployment_apply_network')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'docker/.env').exists())
        (self.root / 'docker/.env').write_text('PUBLIC_ORIGINS=*\n')
        result = self.run_shell('PUBLIC_ORIGIN_FLAG=https://vas.example; deployment_browser_origins')
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required')
    def test_hostname_and_server_ip_are_both_in_certificate(self):
        tls_dir = self.root / 'scripts/tls'; tls_dir.mkdir()
        shutil.copy(REPO / 'scripts/tls/make-internal-ca.sh', tls_dir)
        result = self.run_shell('PUBLIC_ORIGIN_FLAG=https://vas.example; SERVER_IP_FLAG=10.21.5.22; stage_tls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for flag, host in [('-checkhost', 'vas.example'), ('-checkip', '10.21.5.22')]:
            checked = subprocess.run(['openssl', 'x509', '-in', str(self.root/'certs/server.crt'), '-noout', flag, host], capture_output=True)
            self.assertEqual(checked.returncode, 0)

    def test_service_choices_validate_and_persist(self):
        result = self.run_shell('wizard_service_settings; wizard_apply_services',
                               inputs='2\nchat-model:7b\nsql-model:7b\nhttps://vas.example/notebook/\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = (self.root / 'docker/.env').read_text()
        self.assertIn('OLLAMA_MODEL=chat-model:7b', saved)
        self.assertIn('OLLAMA_SQL_MODEL=sql-model:7b', saved)
        self.assertIn('ML_NOTEBOOK_URL=https://vas.example/notebook/', saved)
        for value in ('https://user:pass@vas.example/', 'https://vas.example/?token=secret', 'http://vas.example/', 'https://vas.example/#token'):
            self.assertNotEqual(self.run_shell('wizard_service_valid ML_NOTEBOOK_URL "$URL"', extra={'URL': value}).returncode, 0)

    def test_llm_stage_uses_configured_models_not_old_manifest(self):
        result = self.run_shell('''
            ONLINE=1
            compose() {
                case "$1" in
                    ps) echo ollama ;;
                    config) printf 'services:\n  face_recognition:\n    environment:\n      OLLAMA_MODEL: chat:7b\n      OLLAMA_SQL_MODEL: sql:7b\n' ;;
                    exec) printf 'NAME ID SIZE\nchat:7b 123 4GB\n' ;;
                    *) return 1 ;;
                esac
            }
            compose_mutate() { printf '%s\n' "$*" >> "$CALL_LOG"; }
            stage_ollama_models
        ''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text().strip(), 'exec -T ollama ollama pull sql:7b')

    def test_fresh_deploy_stages_are_sequential_without_repeat(self):
        result = self.run_shell('''
            SUBCOMMAND=deploy
            open_log() { :; }; require_root() { :; }
            cmd_install() { echo install; }
            cmd_start() { echo REPEATED; return 1; }
            stage_build() { echo images; }; stage_db_init() { echo database; }
            backup_existing_database() { echo backup; }; stage_up() { echo services; }
            stage_ollama_models() { echo models; }; stage_health() { echo health; }
            finish_report() { echo report; }
            main
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['install', 'images', 'database', 'backup', 'services', 'models', 'health', 'report'])

    def test_storage_guide_custom_path_is_preview_only(self):
        chosen = str(self.root / 'data disk')
        result = self.run_shell('wizard_storage_settings', inputs=f'2\n{chosen}\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(chosen + '/storage_data', result.stdout)
        self.assertIn(str(self.root / 'weights/det_10g.onnx'), result.stdout)
        self.assertFalse(Path(chosen).exists())
        self.assertFalse((self.root / 'docker/.env').exists())

    def test_storage_overlay_resolves_saved_root_and_passes_it_to_compose(self):
        (self.root / 'docker/.env').write_text("VAS_DATA_ROOT='/srv/vas data'\n")
        result = self.run_shell('''
            docker() { printf '%s\n' "$VAS_DATA_ROOT" "$@"; }
            compose config --quiet
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], '/srv/vas data')
        self.assertIn('docker-compose.prod.storage.yml', result.stdout)

    def test_default_storage_does_not_include_overlay(self):
        result = self.run_shell('unset VAS_DATA_ROOT; compose_files')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('storage.yml', result.stdout)

    def test_storage_choice_persists_for_later_deploy_commands(self):
        result = self.run_shell('''
            export VAS_DATA_ROOT='/srv/vas data'
            wizard_apply_storage
            unset VAS_DATA_ROOT
            storage_root
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '/srv/vas data')
        self.assertEqual((self.root / 'docker/.env').read_text().count('VAS_DATA_ROOT='), 1)

    def test_storage_dry_run_checks_without_preparing_directories(self):
        result = self.run_shell('''
            DRY_RUN=1
            storage_tool() { printf '%s\n' "$1" >> "$CALL_LOG"; }
            stage_storage
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), 'check\n')

    def gpu_fixture(self):
        (self.root / 'docker/Dockerfile.gpu').write_text('FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS base\n')
        return '''
            stage_begin '02 host prerequisites'
            REQUIRE_GPU=1; FORCE_CPU=0; IS_WSL2=0; ONLINE=1
            GPU_DRIVER_MIN=550.54.15; GPU_CUDA_VERSION=12.4.1
            is_windows_shell() { return 1; }
            gpu_boot_id() { echo boot-A; }
            gpu_host_os() { echo ubuntu; }
            gpu_hardware_present() { return 0; }
            uname() { case "$1" in -m) echo x86_64 ;; -r) echo fixture-kernel ;; esac; }
            apt-get() { printf 'apt-get %s\n' "$*" >> "$CALL_LOG"; }
            apt-cache() { echo '  Candidate: 580.82.09-0ubuntu'; }
            ubuntu-drivers() { echo 'driver : nvidia-driver-580-open - distro non-free recommended'; }
        '''

    def test_driver_compatibility_floor(self):
        for version, compatible in [('535.183.01', False), ('550.54.14', False),
                                    ('550.54.15', True), ('580.82.09', True), ('bad', False)]:
            result = self.run_shell('gpu_driver_version_ok "$VERSION" 550.54.15', extra={'VERSION': version})
            self.assertEqual(result.returncode == 0, compatible, version)

    def test_unknown_cuda_image_requires_policy_review(self):
        self.gpu_fixture()
        (self.root / 'docker/Dockerfile.gpu').write_text('FROM nvidia/cuda:99.1-runtime\n')
        result = self.run_shell('gpu_cuda_contract')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unreviewed CUDA image', result.stderr)

    def test_gpu_verify_never_installs_or_restarts(self):
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=verify
            gpu_driver_version() { echo 535.183.01; }
            systemctl() { touch "$ROOT/restarted"; }
            stage_gpu_host_setup 1
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / 'restarted').exists())
        self.assertIn('does not meet', result.stderr)

    def test_compatible_driver_is_not_upgraded(self):
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=install
            gpu_driver_version() { echo 580.82.09; }
            have() { [ "$1" = nvidia-ctk ]; }
            gpu_docker_runtime_ready() { return 0; }
            stage_gpu_host_setup 1
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())
        self.assertIn('Keeping compatible', result.stdout)

    def test_driver_install_stops_for_reboot_before_app_start(self):
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=install
            gpu_driver_version() { return 1; }
            have() { return 1; }
            stage_gpu_host_setup 1
            touch "$ROOT/app-started"
        ''')
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn('linux-headers-fixture-kernel nvidia-driver-580-open', self.log.read_text())
        self.assertIn('REBOOT_REQUIRED', result.stdout)
        self.assertFalse((self.root / 'app-started').exists())
        # Same boot: must not install twice or pretend setup completed.
        self.log.unlink()
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=install
            gpu_driver_version() { return 1; }
            stage_gpu_host_setup 1
        ''')
        self.assertEqual(result.returncode, 75)
        self.assertFalse(self.log.exists())

    def test_reboot_with_failed_driver_does_not_reinstall_loop(self):
        result = self.run_shell(self.gpu_fixture() + '''
            state_set gpu_driver_pending_boot boot-before
            GPU_SETUP_POLICY=install
            gpu_driver_version() { return 1; }
            stage_gpu_host_setup 1
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('after reboot', result.stderr)
        self.assertFalse(self.log.exists())

    def test_driver_install_refuses_offline_wsl_and_unsupported_os(self):
        scenarios = ["ONLINE=0", "IS_WSL2=1", "gpu_host_os() { echo debian; }"]
        for scenario in scenarios:
            result = self.run_shell(self.gpu_fixture() + '''
                GPU_SETUP_POLICY=install
                gpu_driver_version() { return 1; }
            ''' + scenario + '\nstage_gpu_host_setup 0\n')
            self.assertNotEqual(result.returncode, 0, scenario)
            self.assertFalse(self.log.exists(), scenario)

    def test_driver_install_dry_run_is_inert(self):
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=install; DRY_RUN=1
            gpu_driver_version() { return 1; }
            stage_gpu_host_setup 0
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Preview', result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / '.deployment').exists())

    def test_driver_package_must_be_supported_and_compatible(self):
        for setting in ['NVIDIA_DRIVER_PACKAGE=nvidia-driver-590',
                        "apt-cache() { echo '  Candidate: 535.183.01-0ubuntu'; }"]:
            result = self.run_shell(self.gpu_fixture() + '''
                GPU_SETUP_POLICY=install
                gpu_driver_version() { return 1; }
            ''' + setting + '\nstage_gpu_host_setup 1\n')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('linux-headers', self.log.read_text())
            self.log.unlink()

    def test_toolkit_repair_preserves_driver_and_checks_restart(self):
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=install
            gpu_driver_version() { echo 580.82.09; }
            CTK_READY=0; RUNTIME_READY=0
            have() { [ "$1" = nvidia-ctk ] && [ "$CTK_READY" = 1 ]; }
            gpu_docker_runtime_ready() { [ "$RUNTIME_READY" = 1 ]; }
            install_nvidia_toolkit_online() { echo toolkit-installed >> "$CALL_LOG"; CTK_READY=1; }
            nvidia-ctk() { printf 'ctk %s\n' "$*" >> "$CALL_LOG"; }
            systemctl() { echo docker-restarted >> "$CALL_LOG"; RUNTIME_READY=1; }
            stage_gpu_host_setup 1
        ''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ['toolkit-installed', 'ctk runtime configure --runtime=docker', 'docker-restarted'])

    def test_explicit_gpu_does_not_fall_back_to_cpu(self):
        result = self.run_shell('REQUIRE_GPU=1; gpu_inventory() { :; }; stage_gpu_detect')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CPU fallback is not accepted', result.stderr)

    def test_successful_reboot_continues_without_reinstall(self):
        result = self.run_shell(self.gpu_fixture() + '''
            state_set gpu_driver_pending_boot boot-before
            GPU_SETUP_POLICY=verify
            gpu_driver_version() { echo 580.82.09; }
            have() { [ "$1" = nvidia-ctk ]; }
            gpu_docker_runtime_ready() { return 0; }
            stage_gpu_host_setup 0
            test -z "$(state_get gpu_driver_pending_boot)"
        ''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.log.exists())

    def test_failed_docker_restart_is_not_reported_ready(self):
        result = self.run_shell(self.gpu_fixture() + '''
            GPU_SETUP_POLICY=install
            gpu_driver_version() { echo 580.82.09; }
            have() { [ "$1" = nvidia-ctk ]; }
            gpu_docker_runtime_ready() { return 1; }
            nvidia-ctk() { return 0; }
            systemctl() { return 1; }
            stage_gpu_host_setup 1
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Docker restart failed', result.stderr)

    def test_gpu_acceptance_uses_existing_application_container(self):
        self.gpu_fixture()
        result = self.run_shell('''
            GPU_MODE=1
            fake_smi() { echo 580.82.09; }; NVIDIA_SMI=fake_smi
            docker() { echo 'UNEXPECTED IMAGE OPERATION'; return 1; }
            compose() {
                if [ "$1" = ps ]; then echo face_recognition; else printf '%s\n' "$*" >> "$CALL_LOG"; fi
            }
            real_inference_test() { echo 'real-inference' >> "$CALL_LOG"; }
            stage_gpu_test
        ''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('UNEXPECTED', result.stdout)
        self.assertEqual(self.log.read_text().splitlines(), ['exec -T face_recognition nvidia-smi', 'real-inference'])

    def test_noninteractive_wizard_fails_with_cli_guidance(self):
        result = subprocess.run(['bash', str(self.root / 'deploy.sh'), 'wizard'],
                                input='', text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires a terminal', result.stderr)
        self.assertFalse((self.root / '.deployment').exists())


if __name__ == '__main__':
    unittest.main()
