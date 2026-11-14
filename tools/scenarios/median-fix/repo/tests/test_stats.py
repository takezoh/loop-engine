import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stats import median


class MedianTest(unittest.TestCase):
    def test_odd(self):
        self.assertEqual(2, median([3, 1, 2]))
