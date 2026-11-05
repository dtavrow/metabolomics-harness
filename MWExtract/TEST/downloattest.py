# download_study.py
import os
from ftplib import FTP
from pathlib import Path

HOST = "www.metabolomicsworkbench.org"
REMOTE_DIR = "/Studies"
ARCHIVE = "ST004496_Rawfiles.zip"
OUTPUT = Path(ARCHIVE)
PARTIAL = OUTPUT.with_suffix(OUTPUT.suffix + ".part")

email = os.environ["MWB_EMAIL"]

with FTP(HOST, timeout=600) as ftp:
    ftp.login("anonymous", email)
    ftp.set_pasv(True)
    ftp.cwd(REMOTE_DIR)

    expected_size = ftp.size(ARCHIVE)
    offset = PARTIAL.stat().st_size if PARTIAL.exists() else 0

    print(f"Remote size: {expected_size:,} bytes")
    print(f"Starting at: {offset:,} bytes")

    with PARTIAL.open("ab") as output:
        def write_chunk(data):
            output.write(data)
            print(
                f"\rDownloaded: {output.tell():,} / {expected_size:,} bytes",
                end="",
                flush=True,
            )

        ftp.retrbinary(
            f"RETR {ARCHIVE}",
            write_chunk,
            blocksize=1024 * 1024,
            rest=offset or None,
        )

print()

if PARTIAL.stat().st_size != expected_size:
    raise RuntimeError(
        f"Incomplete download: {PARTIAL.stat().st_size:,} of {expected_size:,} bytes"
    )

PARTIAL.replace(OUTPUT)
print(f"Saved: {OUTPUT.resolve()}")
