import json
import os
import unittest
from unittest.mock import patch
from ops import gateway


class GatewayTests(unittest.TestCase):
    def test_reject_command_injection_before_any_host_access(self):
        for value in ["sh", "run;id", "status --privileged", "fetch ../secret", "desktop;id", "desktop --host other", ""]:
            with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": value}), patch.object(gateway.Path, "read_text") as read:
                with self.assertRaises(ValueError):
                    gateway.main()
                read.assert_not_called()

    def test_reject_missing_limits_or_additional_host_mount(self):
        config = {"mount": "/mnt/volume", "volume_id": "uuid", "workspace": "/mnt/volume/usevps", "container": "test-worker"}
        host = {"Privileged": False, "ReadonlyRootfs": True, "Memory": 3 * 1024 ** 3,
                "MemorySwap": 3 * 1024 ** 3, "PidsLimit": 256, "CpuShares": 128,
                "OomScoreAdj": 500,
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]}
        cases = [({**host, "Memory": 0}, [{"Destination": "/workspace", "Source": config["workspace"]}]),
                 ({**host, "MemorySwap": -1}, [{"Destination": "/workspace", "Source": config["workspace"]}]),
                 (host, [{"Destination": "/workspace", "Source": config["workspace"]}, {"Destination": "/var/run/docker.sock", "Source": "/var/run/docker.sock"}])]
        for h, mounts in cases:
            info = [{"HostConfig": h, "Mounts": mounts, "State": {"Health": {"Status": "healthy"}}}]
            with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "run"}), patch.object(gateway.Path, "read_text", return_value=json.dumps(config)), patch.object(gateway.subprocess, "check_output", side_effect=["uuid rw,prjquota", "container", json.dumps(info)]), patch.object(gateway.os, "execvp") as execute:
                with self.assertRaises(ValueError):
                    gateway.main()
                execute.assert_not_called()

    def test_accept_only_workspace_and_readonly_capacity_probe(self):
        config = {"mount": "/mnt/volume", "volume_id": "uuid", "workspace": "/mnt/volume/usevps", "container": "test-worker"}
        host = {"Privileged": False, "ReadonlyRootfs": True, "Memory": 5 * 1024 ** 3,
                "MemorySwap": 5 * 1024 ** 3, "PidsLimit": 256, "CpuShares": 128,
                "OomScoreAdj": 0, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]}
        mounts = [{"Type": "bind", "Destination": "/workspace", "Source": config["workspace"], "RW": True},
                  {"Type": "bind", "Destination": "/volume-stat", "Source": "/mnt/volume/.usevps-stat", "RW": False},
                  {"Type": "bind", "Destination": "/usr", "Source": "/mnt/volume/.usevps-dev/usr", "RW": False},
                  {"Type": "bind", "Destination": "/bin", "Source": "/mnt/volume/.usevps-dev/usr/bin", "RW": False},
                  {"Type": "bind", "Destination": "/sbin", "Source": "/mnt/volume/.usevps-dev/usr/sbin", "RW": False},
                  {"Type": "bind", "Destination": "/lib", "Source": "/mnt/volume/.usevps-dev/usr/lib", "RW": False},
                  {"Type": "bind", "Destination": "/lib64", "Source": "/mnt/volume/.usevps-dev/usr/lib64", "RW": False},
                  {"Type": "bind", "Destination": "/etc", "Source": "/mnt/volume/.usevps-dev/etc", "RW": False},
                  {"Type": "bind", "Destination": "/opt", "Source": "/mnt/volume/.usevps-dev/opt", "RW": False}]
        good = {'/tmp': gateway.DESKTOP_TMPFS}
        cases = [('status', False, {}, 256, True), ('status', False, good, 512, True),
                 ('desktop', False, good, 512, True), ('desktop', True, good, 512, False),
                 ('desktop', False, {}, 256, False), ('desktop', False, good, 256, False),
                 ('status', False, {}, 512, False), ('status', False, good, 768, False),
                 ('status', False, {'/tmp': gateway.DESKTOP_TMPFS + ',size=1g'}, 512, False),
                 ('desktop', False, {**good, '/run': 'rw'}, 512, False)]
        cases = [(*row, None) for row in cases] + [
            ('status', False, good, 768, True, 768),
            ('desktop', False, good, 768, True, 768),
            ('desktop', False, good, 512, False, 768),
            ('status', False, good, 769, False, 768),
            ('status', False, good, 768, False, 1024),
            ('status', False, good, 512, False, True),
            ('status', False, good, 512, False, '768')]
        cases = [(*row, None, 5 * 1024 ** 3) for row in cases] + [
            ('status', False, good, 512, False, None, None, 48 * 1024 ** 3),
            ('status', False, good, 768, True, 768, 48 * 1024 ** 3, 48 * 1024 ** 3),
            ('desktop', False, good, 768, True, 768, 48 * 1024 ** 3, 48 * 1024 ** 3),
            ('status', False, good, 768, False, 768, 48 * 1024 ** 3, 49 * 1024 ** 3),
            ('status', False, good, 768, False, 768, 49 * 1024 ** 3, 48 * 1024 ** 3),
            ('status', False, good, 512, False, None, True, 5 * 1024 ** 3),
            ('status', False, good, 512, False, None, '48', 5 * 1024 ** 3)]
        for operation, writable, temporary, pids, allowed, configured, memory_ceiling, memory in cases:
            config.pop('pids_limit', None)
            config.pop('memory_limit_bytes', None)
            if configured is not None:
                config['pids_limit'] = configured
            if memory_ceiling is not None:
                config['memory_limit_bytes'] = memory_ceiling
            host['Memory'] = host['MemorySwap'] = memory
            mounts[1]["RW"] = writable
            host['Tmpfs'] = temporary
            host['PidsLimit'] = pids
            info = [{"HostConfig": host, "Mounts": mounts, "State": {"Health": {"Status": "healthy"}}}]
            with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": operation}), patch.object(gateway.Path, "read_text", return_value=json.dumps(config)), patch.object(gateway.subprocess, "check_output", side_effect=["uuid rw,prjquota", "container", json.dumps(info)]), patch.object(gateway.os, "execvp") as execute:
                if not allowed:
                    with self.assertRaises(ValueError):
                        gateway.main()
                    execute.assert_not_called()
                else:
                    with patch.object(gateway, 'server_identity', return_value='a'*64):
                        gateway.main()
                    self.assertEqual(execute.call_args.args[1][-1], operation)
                    self.assertIn('USEVPS_SERVER_ID='+'a'*64, execute.call_args.args[1])

    def test_server_identity_is_opaque_and_stable_across_teams(self):
        with patch.object(gateway.Path, 'read_bytes', return_value=b'a'*32):
            identity=gateway.server_identity()
            self.assertEqual(len(identity),64)
            self.assertEqual(identity,gateway.server_identity())
        with patch.object(gateway.Path, 'read_bytes', return_value=b''):
            with self.assertRaises(ValueError):gateway.server_identity()
