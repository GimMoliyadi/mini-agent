"""Deterministic repository fixtures for the Phase 18 navigation eval."""

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FixtureSpec:
    name: str
    file_count: int
    target_file: str
    error_file: str
    symbol: str = "calculate_discount"
    error_string: str = "invalid discount rate"

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


SPECS = {
    "SMALL": FixtureSpec(
        name="SMALL",
        file_count=7,
        target_file="src/pricing.py",
        error_file="src/validation.py",
    ),
    "MEDIUM": FixtureSpec(
        name="MEDIUM",
        file_count=25,
        target_file="src/services/billing/discounts.py",
        error_file="src/services/billing/validation.py",
    ),
    "LARGE-SYNTHETIC": FixtureSpec(
        name="LARGE-SYNTHETIC",
        file_count=75,
        target_file="src/domain/commerce/pricing/discounts.py",
        error_file="src/domain/commerce/pricing/validation.py",
    ),
}


def get_spec(name: str) -> FixtureSpec:
    try:
        return SPECS[name.upper()]
    except KeyError as exc:
        raise ValueError(f"未知 fixture 规模：{name}") from exc


def _target_source(spec: FixtureSpec) -> str:
    return '''"""Discount calculation used by the navigation fixture."""


def calculate_discount(price: float, rate: float) -> float:
    """Return the price after applying a fractional discount."""
    return price * rate
'''


def _validation_source(spec: FixtureSpec) -> str:
    return f'''"""Input validation for {spec.name.lower()} pricing."""


def validate_discount_rate(rate: float) -> None:
    if not 0 <= rate <= 1:
        raise ValueError("{spec.error_string}")
'''


def _test_source(spec: FixtureSpec) -> str:
    module = spec.target_file[:-3].replace("/", ".")
    return f'''import unittest

from {module} import calculate_discount


class DiscountTests(unittest.TestCase):
    def test_calculate_discount_applies_rate(self):
        self.assertEqual(calculate_discount(100, 0.2), 80)


if __name__ == "__main__":
    unittest.main()
'''


def _filler_paths(spec: FixtureSpec) -> list[str]:
    required = 3  # target, validation, and the focused test
    filler_count = spec.file_count - required
    roots = (
        "src/api",
        "src/core",
        "src/domain",
        "src/infra",
        "src/services",
        "src/workers",
        "tests/support",
    )
    paths = []
    index = 1
    while len(paths) < filler_count:
        root = roots[(index - 1) % len(roots)]
        paths.append(f"{root}/module_{index:02d}.py")
        index += 1
    return paths


def _filler_source(index: int, spec: FixtureSpec) -> str:
    return f'''"""Generated support module {index} for {spec.name.lower()} fixture."""


def helper_{index}(value: int) -> int:
    return value + {index}
'''


def build_fixture(root: str | Path, size: str) -> FixtureSpec:
    """Create one clean, deterministic fixture and return its ground truth."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    spec = get_spec(size)

    files = {
        spec.target_file: _target_source(spec),
        spec.error_file: _validation_source(spec),
        "tests/test_discount.py": _test_source(spec),
    }
    if spec.name == "SMALL":
        files.update(
            {
                "src/app.py": "def app_name():\n    return 'navigation-fixture'\n",
                "src/formatting.py": "def format_price(value):\n    return f'{value:.2f}'\n",
                "src/utils/math_helpers.py": "def add(left, right):\n    return left + right\n",
                "src/utils/strings.py": "def clean(value):\n    return value.strip()\n",
            }
        )
        # The small fixture still has seven files, but the generic validation
        # file is unnecessary noise at this size; keep it as a real target for
        # the error-string variant without changing the symbol task.
    else:
        for index, relative in enumerate(_filler_paths(spec), start=1):
            files[relative] = _filler_source(index, spec)

    if len(files) != spec.file_count:
        raise AssertionError(f"{spec.name} fixture generated {len(files)} files")

    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return spec


def validate_fixture(root: str | Path, spec: FixtureSpec) -> None:
    """Fail loudly if a fixture drifts from its declared ground truth."""
    root = Path(root).resolve()
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    if len(files) != spec.file_count:
        raise AssertionError(f"{spec.name} expected {spec.file_count} files, got {len(files)}")
    for relative in (spec.target_file, spec.error_file, "tests/test_discount.py"):
        if relative not in files:
            raise AssertionError(f"{spec.name} missing required file: {relative}")
    definition_hits = sum(
        f"def {spec.symbol}" in (root / relative).read_text(encoding="utf-8")
        for relative in files
    )
    if definition_hits != 1:
        raise AssertionError(f"{spec.name} expected one symbol definition, got {definition_hits}")
    error_hits = sum(
        spec.error_string in (root / relative).read_text(encoding="utf-8") for relative in files
    )
    if error_hits != 1:
        raise AssertionError(f"{spec.name} expected one error-string file, got {error_hits}")


def coding_contract(spec: FixtureSpec) -> dict:
    return {
        "task_id": f"navigation_{spec.name.lower().replace('-', '_')}",
        "instruction": "修复 calculate_discount 的错误，让相关测试通过。",
        "allowed_paths": [spec.target_file],
        "test_command": {
            "command": "python",
            "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"],
            "cwd": ".",
        },
        "require_test_pass": True,
    }
