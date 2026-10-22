import json
import os
import tempfile
import unittest

import runtime


class RuntimeTests(unittest.TestCase):
    def test_atomic_write_replaces_whole_file_without_temp_leftovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'signals.json')
            runtime.atomic_write_json(path, {'version': 1, 'text': '旧'})
            runtime.atomic_write_json(path, {'version': 2, 'text': '新'})
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), {'version': 2, 'text': '新'})
            self.assertEqual([name for name in os.listdir(tmp) if name.startswith('.tmp-')], [])

    def test_single_instance_lock_can_be_reacquired_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'radar.lock')
            with runtime.single_instance_lock(path):
                self.assertTrue(os.path.exists(path))
            with runtime.single_instance_lock(path):
                pass


if __name__ == '__main__':
    unittest.main()
