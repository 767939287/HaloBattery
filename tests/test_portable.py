import os
import sys
import tempfile
import unittest
from unittest import mock

# Add the project root to the path so we can import halo_battery
sys.path.insert(0, os.path.dirname(__file__) + "..")

# Import the function we want to test
from halo_battery import _calculate_data_dir

class PortableTest(unittest.TestCase):
    
    def test_portable_mode_disabled_when_file_absent(self):
        """Test that portable mode is disabled when portable.txt is not present."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Call the function directly with temp directory
            with mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\test\\AppData\\Roaming"}):
                portable_mode, data_dir = _calculate_data_dir(temp_dir)
                expected_dir = os.path.join("C:\\Users\\test\\AppData\\Roaming", "HaloBattery")
                self.assertFalse(portable_mode)
                self.assertEqual(data_dir, expected_dir)

    def test_portable_mode_enabled_when_file_exists(self):
        """Test that portable mode is enabled when portable.txt exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create portable.txt in the directory
            portable_file = os.path.join(temp_dir, 'portable.txt')
            with open(portable_file, 'w') as f:
                f.write('test content')
            
            # Call the function directly with temp directory
            portable_mode, data_dir = _calculate_data_dir(temp_dir)
            self.assertTrue(portable_mode)
            self.assertEqual(data_dir, temp_dir)

    def test_data_dir_selection_normal_mode(self):
        """Test that in normal mode, DATA_DIR uses APPDATA directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Ensure portable.txt does not exist in the directory
            portable_file = os.path.join(temp_dir, 'portable.txt')
            self.assertFalse(os.path.exists(portable_file))
            
            # Call the function directly with temp directory
            with mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\test\\AppData\\Roaming"}):
                portable_mode, data_dir = _calculate_data_dir(temp_dir)
                expected_dir = os.path.join("C:\\Users\\test\\AppData\\Roaming", "HaloBattery")
                self.assertFalse(portable_mode)
                self.assertEqual(data_dir, expected_dir)

if __name__ == '__main__':
    unittest.main()
