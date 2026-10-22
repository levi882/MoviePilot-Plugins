import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import urllib.request


def fetch_dovi_build():
    if platform.machine() != "x86_64":
        raise RuntimeError("This worker image supports Linux x86_64 only")
    root = Path("/opt/gb1")
    url = "https://github.com/quietvoid/dovi_tool/releases/download/2.3.4/dovi_tool-2.3.4-x86_64-unknown-linux-musl.tar.gz"
    expected = "1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("dovi_tool release checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "dovi_tool"]
        if len(members) != 1:
            raise RuntimeError("Unexpected release archive")
        binary = root / "dovi_tool"
        binary.write_bytes(archive.extractfile(members[0]).read())
        binary.chmod(0o755)
    provenance = root / "source/dovi_tool"
    provenance.mkdir(parents=True, exist_ok=True)
    license_url = "https://raw.githubusercontent.com/quietvoid/dovi_tool/2.3.4/LICENSE"
    with urllib.request.urlopen(license_url, timeout=60) as response:
        (provenance / "LICENSE").write_bytes(response.read())
    (provenance / "UPSTREAM.json").write_text(json.dumps({"url": url, "sha256": expected,
                                                          "version": "2.3.4", "license_url": license_url}, indent=2))

if __name__ == "__main__":
    fetch_dovi_build()
