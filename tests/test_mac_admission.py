import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('mac_admission',
    Path(__file__).resolve().parents[1] / 'macos/compute/admission.py')
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


class AdmissionTests(unittest.TestCase):
    def test_spare_resources_and_pressure_gate(self):
        state = {'cores': 8, 'load': 3.2, 'total': 24 * admission.GIB,
                 'percent': 60, 'reclaimable': 10 * admission.GIB}
        self.assertEqual(admission.available({'cpu': 3, 'ram_mib': 3072}, **state), (True, None))
        self.assertEqual(admission.available({'cpu': 4, 'ram_mib': 3072}, **state), (False, 'CPU busy'))
        self.assertEqual(admission.available({'cpu': 1, 'ram_mib': 3232}, **state), (False, 'RAM unavailable'))
        self.assertEqual(admission.available({'cpu': 1, 'ram_mib': 4097}, **state), (False, 'RAM unavailable'))
        self.assertEqual(admission.available({'cpu': 1, 'ram_mib': 512}, **{**state, 'percent': 20}),
                         (False, 'memory pressure'))

    def test_missing_memory_data_fails_closed(self):
        stats = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
                 'Pages free: 100.\nPages inactive: 300.\nPages speculative: 50.\n')
        self.assertEqual(admission.memory_stats('System-wide memory free percentage: 50%', stats,
                                                1024 ** 3), (50, 450 * 16384))
        with self.assertRaises(ValueError):
            admission.memory_stats('unknown', stats, 1024 ** 3)
        with self.assertRaises(ValueError):
            admission.memory_stats('System-wide memory free percentage: 50%', 'incomplete', 1024 ** 3)


if __name__ == '__main__':
    unittest.main()
