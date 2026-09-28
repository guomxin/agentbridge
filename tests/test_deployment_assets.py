from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentAssetTests(unittest.TestCase):
    def test_release_resume_rechecks_version_before_pushing(self) -> None:
        publish = (ROOT / "scripts/Publish-AgentBridge.ps1").read_text(encoding="utf-8")
        acceptance = (ROOT / "scripts/Test-AgentBridgeReleaseAcceptance.ps1").read_text(encoding="utf-8")
        lifecycle = (ROOT / "scripts/Restart-AgentBridgeOpenClawGateway.ps1").read_text(encoding="utf-8")
        self.assertIn("if (-not $ResumeAcceptance)", publish)
        self.assertIn("$reuseValidation = [bool]$SkipValidation -or [bool]$ResumeAcceptance", publish)
        self.assertIn('validationMode = if ($reuseValidation) { "reuse_receipt" } else { "full" }', publish)
        self.assertIn("if (-not $reuseValidation)", publish)
        self.assertNotIn("Test-AgentBridgeCandidate.ps1", publish)
        self.assertIn("ExpectedReleaseId = $commit.Substring(0, 12)", publish)
        self.assertIn("$ResumeAcceptance -and ($RestartOpenClaw -or $IncludeLoginReuseSmoke)", publish)
        self.assertIn("$remote.releaseId -ne $ExpectedReleaseId", acceptance)
        self.assertLess(publish.index("& $releaseAcceptanceScript @acceptanceParameters"),
                        publish.index("push --porcelain"))
        self.assertIn("$deadline = $StartedAt.AddSeconds($ReadyTimeoutSeconds)", lifecycle)
        self.assertIn("$action = \"startup_adopted\"", lifecycle)
        self.assertIn("-StartedAt ([DateTimeOffset]$existingProcesses[0].CreationDate)", lifecycle)
        self.assertIn("Startup phase={0}; elapsedSeconds={1}", lifecycle)

    def test_systemd_service_cannot_import_legacy_app_source(self) -> None:
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(encoding="utf-8")

        self.assertIn("WorkingDirectory=/home/agentbridge/service\n", unit)
        self.assertNotIn("WorkingDirectory=/home/agentbridge/service/app", unit)
        self.assertIn("venv/bin/python -P -m bscli.cli.main", unit)

    def test_deployment_installs_unit_and_checks_runtime_module_source(self) -> None:
        script = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(encoding="utf-8")
        runner = (ROOT / "scripts/agentbridge_release.py").read_text(encoding="utf-8")
        known_hosts = ROOT / "deploy/ssh/agentbridge_known_hosts"

        for marker in (
            "service did not stabilize on the release unit",
            "service resolves unexpected bscli module",
            "UserKnownHostsFile=",
            "SSH known-hosts file was not found",
            "$smokeScript -Check Release",
        ):
            self.assertIn(marker, script + runner)
        self.assertTrue(known_hosts.is_file())
        self.assertIn(
            "10.10.50.213 ssh-ed25519 ",
            known_hosts.read_text(encoding="ascii"),
        )

    def test_publish_entry_is_pinned_to_the_validated_release_path(self) -> None:
        script = (ROOT / "scripts/Publish-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )
        github_known_hosts = ROOT / "deploy/ssh/github_known_hosts"

        for marker in (
            '"git@github.com:guomxin/cli-helper.git"',
            'Join-Path $repoRoot ".gitrepo"',
            'Join-Path $repoRoot ".git"',
            '"The repository metadata directory was not found"',
            'symbolic-ref --short -q HEAD',
            '"Detached release worktree must point to refs/heads/$BranchName"',
            '"Tracked files are modified. Commit the tested candidate before publishing."',
            "Assert-PrivateKeyReadable -Path $GitHubIdentityFile",
            "Assert-PrivateKeyReadable -Path $AgentBridgeIdentityFile",
            "& $validationScript -Mode Full",
            "& $runtimeGovernanceScript",
            "SkipValidation = $true",
            "& $deployScript @deployParameters",
            "& $releaseAcceptanceScript @acceptanceParameters",
            'push --porcelain $RemoteName "HEAD:refs/heads/$BranchName"',
            '"ls-remote", "--exit-code"',
            "GitHub verification mismatch",
        ):
            self.assertIn(marker, script)
        self.assertLess(
            script.index("& $validationScript -Mode Full"),
            script.index("& $runtimeGovernanceScript"),
        )
        self.assertLess(
            script.index("& $runtimeGovernanceScript"),
            script.index("& $deployScript @deployParameters"),
        )
        self.assertLess(
            script.index("& $deployScript @deployParameters"),
            script.index("& $releaseAcceptanceScript @acceptanceParameters"),
        )
        self.assertLess(
            script.index("& $releaseAcceptanceScript @acceptanceParameters"),
            script.index("push --porcelain"),
        )
        self.assertTrue(github_known_hosts.is_file())
        self.assertIn(
            "github.com ssh-ed25519 ",
            github_known_hosts.read_text(encoding="ascii"),
        )

    def test_full_validation_parallelizes_python_without_weakening_receipt(self) -> None:
        script = (ROOT / "scripts/Invoke-AgentBridgeValidation.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('"pytest-xdist"', script)
        self.assertIn('"-n", "4", "--dist", "loadscope"', script)
        self.assertIn('"--junitxml=output/release-validation/pytest.xml"', script)
        self.assertLess(
            script.index('scripts/agentbridge_artifact.py", "begin"'),
            script.index('"-n", "4", "--dist", "loadscope"'),
        )
        self.assertLess(
            script.index('"-n", "4", "--dist", "loadscope"'),
            script.index('scripts/agentbridge_artifact.py", "finish"'),
        )

    def test_backup_units_are_installed_only_after_runtime_readiness(self) -> None:
        deploy = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )
        backup_service = ROOT / "deploy/systemd/agentbridge-backup.service"
        backup_timer = ROOT / "deploy/systemd/agentbridge-backup.timer"

        self.assertTrue(backup_service.is_file())
        self.assertTrue(backup_timer.is_file())
        self.assertIn("agentbridge_release.py", deploy)
        runner = (ROOT / "scripts/agentbridge_release.py").read_text(encoding="utf-8")
        self.assertIn('self.run("systemd-analyze", "verify"', runner)
        self.assertIn('self.run("systemctl", "daemon-reload")', runner)
        self.assertIn('self.stage("confirmed")', runner)
        self.assertIn('("systemctl", "start", self.service + "-backup.service")', runner)

    def test_local_host_self_heal_and_restore_drill_are_bounded(self) -> None:
        installer = (
            ROOT / "scripts/Install-AgentBridgeOpenClawSelfHeal.ps1"
        ).read_text(encoding="utf-8")
        guard = (
            ROOT / "scripts/Start-AgentBridgeOpenClawGuard.ps1"
        ).read_text(encoding="utf-8")
        lifecycle = (
            ROOT / "scripts/Restart-AgentBridgeOpenClawGateway.ps1"
        ).read_text(encoding="utf-8")
        lifecycle_lease = (
            ROOT / "scripts/AgentBridgeOpenClawLifecycleLease.psm1"
        ).read_text(encoding="utf-8")
        foreground = (
            ROOT / "scripts/Invoke-AgentBridgeOpenClawGatewayForeground.ps1"
        ).read_text(encoding="utf-8")
        recovery = (
            ROOT / "scripts/Test-AgentBridgeLocalHostRecovery.ps1"
        ).read_text(encoding="utf-8")
        restore = (
            ROOT / "scripts/Test-AgentBridgeBackupRestore.ps1"
        ).read_text(encoding="utf-8")

        for marker in (
            "AgentBridge visible Gateway task shim",
            "GatewayRuntimeLauncher",
            "AgentBridge OpenClaw Guard",
            "-RestartCount 999",
            "openclaw-guard-hidden.vbs",
            "wscript.exe",
            'shell.Run("{0}", 0, True)',
            "guardConsoleHidden = $true",
            "Stop-ExistingGuardProcesses",
            "stoppedGuardProcessIds = $stoppedGuardProcessIds",
            "did not publish a current lifecycle-aware heartbeat",
            "startupLauncherRetired = $true",
            'mode = "startup_guard_visible_gateway"',
            "visibleForeground = $true",
        ):
            self.assertIn(marker, installer)
        for marker in (
            "AgentBridgeOpenClawGuard",
            "Invoke-GatewayLifecycle",
            "Get-AgentBridgeOpenClawLifecycleLease",
            'gatewayAction = "lifecycle_in_progress"',
            "$replacement = Invoke-GatewayLifecycle -StartOnly",
            "lifecycleHeartbeatAgeSeconds",
            "gatewayListening",
            "businessCalls = 0",
            "businessListReads = 0",
            "businessWrites = 0",
        ):
            self.assertIn(marker, guard)
        for marker in (
            "Remove-StaleGatewayLocks",
            "AgentBridgeOpenClawLifecycle",
            "Set-AgentBridgeOpenClawLifecycleLease",
            'Update-LifecycleLease -Phase $phase',
            'Update-LifecycleLease -State "completed" -Phase "completed"',
            "Start-VisibleGateway",
            "Get-VisibleGatewayForeground",
            "AgentBridgeGateway",
            "WindowsTerminal",
            "visibleForeground = $visibleForeground",
            "businessWrites = 0",
        ):
            self.assertIn(marker, lifecycle)
        for marker in (
            "agentbridge.openclaw-lifecycle-operation.v1",
            'reason = "owner_missing"',
            'reason = "expired"',
            "Move-Item -LiteralPath $tempPath -Destination $Path -Force",
        ):
            self.assertIn(marker, lifecycle_lease)
        self.assertLess(
            guard.index('if ($lifecycleLease.active)'),
            guard.index('elseif ($listener)'),
        )
        for marker in (
            "Test-GatewayVisibleForeground",
            "hidden_gateway_replaced",
        ):
            self.assertIn(marker, guard)
        for marker in (
            'WindowTitle = "AgentBridge OpenClaw Gateway"',
            "Closing it stops the Gateway",
            "exit 0",
        ):
            self.assertIn(marker, foreground)
        for marker in (
            "ExerciseFailureRecovery",
            "ExerciseLifecycleLease",
            "Get-GatewayReadyState",
            "simulated_slow_start",
            'guardAction = [string]$guardLeaseState.gatewayAction',
            "gatewayPreserved = $true",
            'http://127.0.0.1:$GatewayPort/readyz',
            "Get-LatestAgentBridgePluginRegistration",
            "listenerCount = 1",
            "businessCalls = 0",
            "businessListReads = 0",
            "businessWrites = 0",
        ):
            self.assertIn(marker, recovery)
        for marker in (
            "backup-restore-drill",
            "readOnlyOpen",
            "writeRejected",
            "businessWrites -ne 0",
        ):
            self.assertIn(marker, restore)

    def test_release_acceptance_uses_the_tracked_agentbridge_host_key(self) -> None:
        for path in (
            "scripts/Test-AgentBridgeReleaseAcceptance.ps1",
            "scripts/Test-AgentBridgeOmnichannelIsolation.ps1",
        ):
            script = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("KnownHostsFile", script)
            self.assertIn("deploy\\ssh\\agentbridge_known_hosts", script)
            self.assertIn("UserKnownHostsFile=", script)

    def test_openclaw_plugin_inspection_is_bounded_and_one_shot(self) -> None:
        acceptance = (
            ROOT / "scripts/Test-AgentBridgeReleaseAcceptance.ps1"
        ).read_text(encoding="utf-8")
        deploy = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '[ValidateRange(5, 300)][int]$OpenClawTimeoutSeconds = 180',
            acceptance,
        )
        self.assertIn(
            '@("gateway", "status", "--require-rpc", "--json")',
            acceptance,
        )
        self.assertNotIn(
            '@("gateway", "status", "--deep", "--require-rpc", "--json")',
            acceptance,
        )
        self.assertIn("taskkill.exe", acceptance)
        self.assertIn("for ($attempt = 1; $attempt -le 3; $attempt++)", acceptance)
        self.assertNotIn(
            'plugins", "inspect", "agentbridge-interactions", "--runtime"',
            acceptance,
        )
        self.assertNotIn(
            "plugins inspect agentbridge-interactions --runtime",
            deploy,
        )

    def test_admin_console_is_deployed_with_tls_and_release_metadata(self) -> None:
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )
        script = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )

        for marker in (
            "EnvironmentFile=-/home/agentbridge/service/config/release.env",
            "--admin-host 127.0.0.1",
            "--admin-port 8782",
            "--admin-public-base-url https://127.0.0.1:8782",
            "--admin-tls-cert /home/agentbridge/service/config/tls/server.crt",
            "--admin-tls-key /home/agentbridge/service/config/tls/server.key",
        ):
            self.assertIn(marker, unit)
        runner = (ROOT / "scripts/agentbridge_release.py").read_text(encoding="utf-8")
        self.assertIn("AGENTBRIDGE_RELEASE_ID", runner)
        self.assertIn("def atomic_write(path, data, mode=0o640)", runner)

    def test_smartlight_adapter_requires_explicit_plain_http_opt_in(self) -> None:
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "--smartlight-base-url https://lighting.example.test/smartlight",
            unit,
        )
        self.assertNotIn("--smartlight-allow-insecure-http", unit)
    def test_openclaw_restart_has_recovery_guardrails_and_warmup_gate(self) -> None:
        deploy = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )
        warmup = (
            ROOT / "scripts/Test-OpenClawGatewayWarmup.ps1"
        ).read_text(encoding="utf-8")

        for marker in (
            "diagnostics.stuckSessionWarnMs",
            "diagnostics.stuckSessionAbortMs",
            "$diagnosticsAlreadyConfigured",
            "already configured; skipping config write",
            "--batch-file",
            "$gatewayRuntimeScript",
            "Test-AgentBridgeOpenClawRuntime.ps1",
            "OpenClaw Gateway runtime or AgentBridge plugin is not healthy",
            "$gatewayWarmupScript",
            "Complete-AgentBridgeOpenClawWarmup",
        ):
            self.assertIn(marker, deploy)
        policy = (ROOT / "scripts/AgentBridgeOpenClawRestartPolicy.psm1").read_text(encoding="utf-8")
        self.assertIn('if ($result.status -ne "succeeded")', policy)
        self.assertLess(deploy.index("[IO.File]::WriteAllText($gatewayWarmupPendingPath"),
                        deploy.index("& $gatewayLifecycleScript -ReadyTimeoutSeconds"))
        publish = (ROOT / "scripts/Publish-AgentBridge.ps1").read_text(encoding="utf-8")
        self.assertLess(publish.index("Test-AgentBridgePendingOpenClawWarmup.ps1"),
                        publish.index("& $releaseAcceptanceScript @acceptanceParameters"))
        self.assertNotIn("gateway status --deep --require-rpc --json", deploy)
        self.assertNotIn("plugins inspect agentbridge-interactions --json", deploy)

        runtime_check = (
            ROOT / "scripts/Test-AgentBridgeOpenClawRuntime.ps1"
        ).read_text(encoding="utf-8")
        for marker in (
            'http://127.0.0.1:$GatewayPort/readyz',
            "integrations\\openclaw-agentbridge\\package.json",
            "AgentBridge interaction plugin registered",
            "Gateway process within the stabilization window",
            "StabilizationTimeoutSeconds = 45",
            "$readinessAttempts++",
            "$registrationDeadline",
            "plugin runtime version does not match its manifest",
            "pluginRegistrationFreshnessPolicy",
            '"current_gateway_process"',
            "businessWrites = 0",
        ):
            self.assertIn(marker, runtime_check)
        self.assertNotIn("plugin registration is stale", runtime_check)

        guard = (
            ROOT / "scripts/Start-AgentBridgeOpenClawGuard.ps1"
        ).read_text(encoding="utf-8")
        for marker in (
            "GatewayStartupGraceSeconds = 600",
            "startup_in_progress",
            "duplicates_removed",
            "stale_start_replaced",
            "gatewayProcessCount",
        ):
            self.assertIn(marker, guard)

        smoke = (ROOT / "scripts/agentbridge-mcp-smoke.mjs").read_text(
            encoding="utf-8"
        )
        for marker in (
            "OaAddressbookOrganization",
            "OaAddressbookPersonSearch",
            "OaAddressbookGroups",
            "OaAddressbookPrivateContacts",
            "addressbookListSummary",
        ):
            self.assertIn(marker, smoke)
        for marker in (
            'sessionKey = "agent:${AgentId}:agentbridge-release-warmup"',
            '--message $message',
            '--thinking off',
            '$status -ne "ok"',
            '$reply -ne "READY"',
            "HotPathMaximumSeconds",
            '[ValidateRange(1, 900)][int]$HotPathMaximumSeconds = 180',
            'durationBasis = "cli_end_to_end"',
            'hotPathMaximumSeconds = $HotPathMaximumSeconds',
        ):
            self.assertIn(marker, warmup)

    def test_deployment_reuses_verified_candidate_without_building_in_workspace(self) -> None:
        script = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("-m pip wheel", script)
        self.assertIn("verify --root $repoRoot", script)
        runner = (ROOT / "scripts/agentbridge_release.py").read_text(encoding="utf-8")
        self.assertIn('hashlib.sha256(wheel.read_bytes()).hexdigest()', runner)
        # Full receipts exceed Linux's per-argument limit; pass a filename.
        self.assertIn('"$unit_tmp_dir/release.py" "$unit_tmp_dir/config.json"', script)
        self.assertNotIn('"$unit_tmp_dir/release.py" \'\'__RELEASE_CONFIG__', script)
        self.assertLess(script.index("verify --root $repoRoot"), script.index("& $scp.Source"))

    def test_yuque_remote_login_uses_challenge_isolation_and_native_novnc(self) -> None:
        service = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )
        deploy = (ROOT / "scripts/Deploy-AgentBridge.ps1").read_text(
            encoding="utf-8"
        )
        broker = (ROOT / "bscli/broker/remote_browser.py").read_text(
            encoding="utf-8"
        )

        for retired in (
            "Requires=agentbridge-xvfb.service",
            "JoinsNamespaceOf=agentbridge-xvfb.service",
            "Environment=DISPLAY=:99",
            "Environment=XAUTHORITY=",
        ):
            self.assertNotIn(retired, service)
        self.assertFalse(
            (ROOT / "deploy/systemd/agentbridge-xvfb.service").exists()
        )
        for marker in (
            "xvfb x11vnc novnc websockify xauth",
            "test -d /usr/share/novnc",
        ):
            self.assertIn(marker, deploy)
        for marker in (
            "-nolisten",
            '"tcp"',
            '"-localhost"',
            '"--token-plugin=TokenFile"',
            '"--remote-debugging-address=127.0.0.1"',
            'f"--remote-debugging-port={self.allocation.cdp_port}"',
        ):
            self.assertIn(marker, broker)
        for retired in ("--enable-automation", "--remote-debugging-pipe"):
            self.assertNotIn(retired, broker)

    def test_release_smoke_requires_new_write_tools(self) -> None:
        smoke = (ROOT / "scripts/agentbridge-mcp-smoke.mjs").read_text(encoding="utf-8")

        for tool in (
            "oa_certificate_search",
            "oa_certificate_prepare_download",
            "oa_certificate_prepare_downloads",
            "oa_business_trip_prepare",
            "oa_business_trip_save_draft",
            "oa_business_trip_submit_prepare",
            "oa_business_trip_submit",
            "oa_leave_prepare",
            "oa_leave_save_draft",
            "oa_leave_submit_prepare",
            "oa_leave_submit",
            "oa_workflow_revoke_prepare",
            "oa_workflow_revoke",
            "oa_missed_punch_prepare",
            "oa_missed_punch_save_draft",
            "oa_missed_punch_approval_prepare",
            "oa_missed_punch_approval_batch_prepare",
            "oa_missed_punch_approve",
            "oa_efficiency_data_approval_prepare",
            "oa_efficiency_data_approve",
            "oa_travel_expense_approval_prepare",
            "oa_travel_expense_approve",
            "oa_business_trip_approval_prepare",
            "oa_business_trip_approve",
            "oa_labor_contract_renewal_approval_prepare",
            "oa_labor_contract_renewal_approve",
            "oa_intellectual_property_declaration_approval_prepare",
            "oa_intellectual_property_declaration_approve",
            "oa_overtime_approval_prepare",
            "oa_overtime_approve",
            "oa_leave_approval_prepare",
            "oa_leave_approve",
            "oa_resignation_approval_prepare",
            "oa_work_handover_approval_prepare",
            "oa_resignation_approve",
            "oa_work_handover_approve",
            "oa_flight_application_approval_prepare",
            "oa_flight_application_approve",
            "oa_attendance_confirmation_prepare",
            "oa_attendance_confirm",
            "oa_weekly_report_acknowledgement_prepare",
            "oa_weekly_report_acknowledge",
            "oa_standard_collaboration_approval_prepare",
            "oa_standard_collaboration_approve",
            "oa_meeting_room_availability_list",
            "oa_meeting_room_my_applications_list",
            "oa_meeting_room_application_prepare",
            "oa_meeting_room_application_create",
            "oa_meeting_room_application_cancel_prepare",
            "oa_meeting_room_application_cancel",
            "oa_meeting_create_prepare",
            "oa_meeting_create",
            "smartlight_alarm_remark_update_prepare",
            "smartlight_alarm_remark_update",
            "smartlight_alarm_work_area_submit_prepare",
            "smartlight_alarm_work_area_submit",
            "smartlight_alarm_work_area_revoke_prepare",
            "smartlight_alarm_work_area_revoke",
            "smartlight_rtu_alarm_dispose_prepare",
            "smartlight_rtu_alarm_dispose",
        ):
            self.assertIn(tool, smoke)

        for check in (
            "SmartlightSessionStatus",
            "SmartlightOverview",
            "SmartlightRuntimeOverview",
            "SmartlightRtuStatus",
            "SmartlightLampStatus",
            "SmartlightLampAlarms",
            "SmartlightLampAlarmAnalysis",
            "SmartlightAlarmRemark",
            "SmartlightRtuSurvey",
            "ToolCatalog",
        ):
            self.assertIn(check, smoke)
            self.assertIn(check, (ROOT / "scripts/Test-AgentBridgeMcp.ps1").read_text(
                encoding="utf-8"
            ))
        for report_type in (
            "energy_records",
            "energy_analysis",
            "lamp_survey_records",
            "rtu_leakage_alarms",
            "rtu_leakage_analysis",
            "inspection_logs",
            "maintenance_records",
        ):
            self.assertIn(f'"{report_type}"', smoke)
        self.assertIn("result.downstreamTotal", smoke)
        self.assertIn('businessSessionCheck: false', smoke)

    def test_openclaw_config_is_read_as_utf8(self) -> None:
        script = (ROOT / "scripts/Test-AgentBridgeMcp.ps1").read_text(encoding="utf-8")

        for marker in (
            "-Encoding UTF8",
            '"agentbridge-interactions"',
            '"identityBindings"',
            '"mcpUrl"',
        ):
            self.assertIn(marker, script)

    def test_identity_isolation_smoke_selects_named_bindings_without_tokens(self) -> None:
        smoke = (ROOT / "scripts/Test-AgentBridgeMcp.ps1").read_text(encoding="utf-8")
        isolation = (
            ROOT / "scripts/Test-AgentBridgeIdentityIsolation.ps1"
        ).read_text(encoding="utf-8")
        node_smoke = (
            ROOT / "scripts/agentbridge-mcp-smoke.mjs"
        ).read_text(encoding="utf-8")

        for marker in (
            "IdentityLabel",
            "IdentityChannel",
            "IdentitySenderId",
            "did not resolve exactly one active binding",
        ):
            self.assertIn(marker, smoke)
        for marker in (
            "uniqueSubjects",
            "identity changed during the stability check",
            "session is not active",
        ):
            self.assertIn(marker, isolation)
        for marker in (
            "TaihuaSessionStatus",
            "OaPendingRead",
            "TaihuaMyLogs",
            "downstreamPrincipalRef",
            "smartlightUnexpectedTools",
            'effectiveCheck.kind === "session"',
            "payload?.isError",
        ):
            self.assertIn(marker, node_smoke)
        self.assertNotIn("Token =", isolation)
    def test_pending_action_preflight_is_read_only_by_construction(self) -> None:
        script = (
            ROOT / "scripts/validate_oa_pending_actions_preflight.py"
        ).read_text(encoding="utf-8")

        for prepare_function in (
            "prepare_missed_punch_approval",
            "prepare_efficiency_data_approval",
            "prepare_travel_expense_approval",
            "prepare_business_trip_approval",
            "prepare_labor_contract_renewal_approval",
            "prepare_intellectual_property_declaration_approval",
            "prepare_overtime_approval",
            "prepare_leave_approval",
            "prepare_resignation_approval",
            "prepare_work_handover_approval",
            "prepare_flight_application_approval",
            "prepare_attendance_confirmation",
            "prepare_weekly_report_acknowledgement",
            "prepare_standard_collaboration_approval",
        ):
            self.assertIn(prepare_function, script)
        for forbidden_function in (
            "approve_missed_punch_request",
            "approve_efficiency_data",
            "approve_travel_expense",
            "approve_business_trip_request",
            "approve_labor_contract_renewal",
            "approve_intellectual_property_declaration",
            "approve_overtime",
            "approve_leave_request",
            "approve_resignation",
            "approve_work_handover",
            "approve_flight_application",
            "confirm_attendance",
            "acknowledge_weekly_report",
            "approve_standard_collaboration",
        ):
            self.assertNotIn(forbidden_function, script)
        self.assertIn('"write_controls_clicked": 0', script)
        self.assertIn('"collaboration_write_requests": 0', script)
        self.assertIn('"authorizations_created": 0', script)
        self.assertNotIn("state_store.save", script)

    def test_workspace_reverse_tunnel_is_loopback_only_and_persistent(self) -> None:
        tunnel = (ROOT / "scripts/Start-AgentBridgeWorkspaceTunnel.ps1").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "scripts/Install-AgentBridgeWorkspaceTunnel.ps1").read_text(
            encoding="utf-8"
        )
        server_installer = (
            ROOT / "scripts/Install-AgentBridgeWorkspaceTunnelServer.ps1"
        ).read_text(encoding="utf-8")
        server_config = (
            ROOT / "deploy/ssh/agentbridge_workspace_tunnel_sshd.conf"
        ).read_text(encoding="ascii")
        guard = (ROOT / "scripts/Start-AgentBridgeOpenClawGuard.ps1").read_text(
            encoding="utf-8"
        )
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )

        for marker in (
            "ExitOnForwardFailure=yes",
            "ServerAliveInterval=10",
            "ServerAliveCountMax=2",
            '127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}',
            "AgentBridgeWorkspaceTunnel",
            "Get-ExistingTunnelProcess",
            "Get-NetworkFingerprint",
            '"network_change_detected"',
            '"active_network_changed"',
            '"connected"',
            "$sshProcess.WaitForExit($NetworkPollSeconds * 1000)",
            '"existing_tunnel_observed"',
            '"workspace-tunnel-status.json"',
            "ResumeGapThresholdSeconds",
            '"resume_detected"',
            '"system_resume_or_long_pause"',
            "StatusHeartbeatSeconds",
            "Complete-SshAttemptLog",
            '"remote_forward_conflict"',
            "-RedirectStandardError $sshAttemptErrorPath",
            "Add-Content -LiteralPath $sshErrorPath",
        ):
            self.assertIn(marker, tunnel)
        self.assertNotIn("Remove-Item -LiteralPath $sshErrorPath -Force", tunnel)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", installer)
        self.assertIn("-WindowStyle Hidden", installer)
        self.assertIn("Get-CimInstance Win32_Process", installer)
        self.assertIn("Stop-Process -Id $_.ProcessId", installer)
        self.assertLess(
            installer.index("Stop-ScheduledTask -TaskName $TaskName"),
            installer.index("Register-ScheduledTask"),
        )
        self.assertIn('$scriptMarker = "-File `"$tunnelScript`""', installer)
        for marker in (
            "TunnelStatusMaxAgeSeconds",
            "Get-TunnelStatus",
            "Stop-RecordedTunnelProcess",
            "Stop-RecordedTunnelWrapperProcess",
            '"stale_status_restarted"',
            "tunnelStatusAgeSeconds",
            "tunnelSshProcessId",
        ):
            self.assertIn(marker, guard)
        for marker in (
            "sshd -t",
            "systemctl reload ssh.service",
            "systemctl reload sshd.service",
            "businessWrites = 0",
        ):
            self.assertIn(marker, server_installer)
        for marker in (
            "Match User root",
            "ClientAliveInterval 5",
            "ClientAliveCountMax 2",
            "Match all",
        ):
            self.assertIn(marker, server_config)
        self.assertIn("--workspace-gateway-url ws://127.0.0.1:18789", unit)
        self.assertNotIn("--workspace-gateway-url ws://10.90.20.210:18789", unit)

    def test_unit_document_probe_is_read_only_by_construction(self) -> None:
        script = (
            ROOT / "scripts/inspect_oa_unit_documents.py"
        ).read_text(encoding="utf-8")

        for marker in (
            '"downloads_started": 0',
            '"write_controls_clicked": 0',
            '"response_bodies_included": False',
            'method in {"DELETE", "PATCH", "PUT"}',
            "_BLOCKED_WRITE_MARKERS",
        ):
            self.assertIn(marker, script)
        for forbidden_function in (
            "expect_download",
            "state_store.save",
            "set_input_files",
            "request.post(",
            "request.put(",
        ):
            self.assertNotIn(forbidden_function, script)

if __name__ == "__main__":
    unittest.main()
