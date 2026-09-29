import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from simple_math import add, double  # noqa: E402


class SimpleMathTests(unittest.TestCase):
    def test_adds_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_doubles_numbers(self) -> None:
        self.assertEqual(double(4), 8)


if __name__ == "__main__":
    unittest.main()
