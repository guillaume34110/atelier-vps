import json
import os
import unittest
from unittest.mock import patch
from ops import gateway


class GatewayTests(unittest.TestCase):
    def test_reject_command_injection_before_any_host_access(self):
        for value in ["sh", "run;id", "status --privileged", "fetch ../secret", ""]:
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
