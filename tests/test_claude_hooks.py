"""Module for testing forge-continuation hooks."""

from pathlib import Path

import pytest


@pytest.fixture
def bin_with_fake_forge_continuation(tmp_path: Path) -> Path:
    """Fake bin dir with forge-continuation executable.

    Returns:
        The bin directory path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "forge-continuation"
    shim.write_text("#!/bin/sh\necho forge-continuation-marker\nexit 0")
    shim.chmod(0o755)

    """A fake bin dir containing an executable forge-continuation that echoes a marker.

    Returns:
        The bin directory path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "forge-continuation"
    shim.write_text("""#!/bin/sh
echo forge-continuation-marker
exit 0
""")
    shim.chmod(0o755)
    return bin_dir
