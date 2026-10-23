import unittest
from ops.prepare_volume import fstab_with_quota, MOUNT


class FstabTests(unittest.TestCase):
    def test_preserve_other_disks_and_idempotent(self):
        original = "UUID=root / ext4 defaults 0 1\n/dev/disk/by-id/volume " + str(MOUNT) + " ext4 discard,nofail,defaults 0 0\n"
        result = fstab_with_quota(original)
        self.assertIn("UUID=root / ext4 defaults 0 1", result)
        self.assertIn("discard,nofail,defaults,prjquota", result)
        self.assertEqual(fstab_with_quota(result), result)

    def test_missing_or_duplicate_mount_is_rejected(self):
        line = "UUID=volume " + str(MOUNT) + " ext4 defaults 0 0\n"
        for value in ("UUID=root / ext4 defaults 0 1\n", line + line):
            with self.assertRaises(ValueError):
                fstab_with_quota(value)
