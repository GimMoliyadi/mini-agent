import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing import calculate_discount  # noqa: E402


class PricingTests(unittest.TestCase):
    def test_calculate_discount(self):
        self.assertEqual(calculate_discount(100, 0.2), 80)


if __name__ == "__main__":
    unittest.main()
