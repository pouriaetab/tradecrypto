"""The generated LaunchAgent must be a valid plist.

2026-09-18: a sentence in an XML comment inside the plist contained a double
hyphen — illegal in XML — so the whole file stopped parsing. launchctl's only
report was:

    Load failed: 5: Input/output error

which is indistinguishable from a permissions problem, and very nearly cost a
grant of Full Disk Access to /bin/bash for a typo.

A config file you generate is code. Parse it before you ship it.
"""
from __future__ import annotations

import plistlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install-autostart.sh"


def _render(program_args: str) -> bytes:
    """Pull the heredoc out of the installer and fill it in, as bash would."""
    text = INSTALLER.read_text()
    m = re.search(r'cat > "\$PLIST" <<PLISTEOF\n(.*?)\nPLISTEOF', text, re.S)
    assert m, "could not find the plist heredoc in the installer"
    body = m.group(1)
    subs = {
        "$LABEL": "com.tradecrypto.app",
        "$PROJECT": "/Users/you/projects/tradecrypto",
        "$HOME": "/Users/you",
        "$PROGRAM_ARGS": program_args,
    }
    for k, v in subs.items():
        body = body.replace(k, v)
    return body.encode()


PLAIN = ("    <string>/bin/bash</string>\n"
         "    <string>/Users/you/projects/tradecrypto/scripts/supervise.sh</string>")
AWAKE = ("    <string>/usr/bin/caffeinate</string>\n    <string>-s</string>\n" + PLAIN)


@pytest.mark.parametrize("args,label", [(PLAIN, "default"), (AWAKE, "--awake")])
def test_the_generated_plist_parses(args, label):
    plistlib.loads(_render(args))          # raises if the XML is malformed


@pytest.mark.parametrize("args", [PLAIN, AWAKE])
def test_it_carries_the_settings_that_make_it_work(args):
    d = plistlib.loads(_render(args))
    assert d["RunAtLoad"] is True
    # KeepAlive must be PathState and NOTHING else: the dictionary is OR'd, so a
    # second condition would revive the desk even while deliberately paused.
    ka = d["KeepAlive"]
    assert set(ka) == {"PathState"}, f"KeepAlive has extra conditions: {set(ka)}"
    (path, keep_while), = ka["PathState"].items()
    assert path.endswith("data/STOP_SUPERVISOR")
    assert keep_while is False, "must be kept alive while the file is ABSENT"


def test_no_xml_comment_in_the_plist_contains_a_double_hyphen():
    """The exact 2026-09-18 failure, asserted directly — because the parser error
    it produces points at a column, not at the reason."""
    body = _render(PLAIN).decode()
    for comment in re.findall(r"<!--(.*?)-->", body, re.S):
        assert "--" not in comment, f"'--' is illegal inside an XML comment: {comment[:80]!r}"


def test_the_awake_variant_actually_wraps_in_caffeinate():
    d = plistlib.loads(_render(AWAKE))
    assert d["ProgramArguments"][0] == "/usr/bin/caffeinate"
    assert d["ProgramArguments"][-1].endswith("supervise.sh")


def test_the_installer_validates_before_loading():
    """A generated config must be parsed before it is installed, and a load
    failure must report the real error rather than being swallowed."""
    text = INSTALLER.read_text()
    assert "plutil -lint" in text
    assert "REFUSING TO INSTALL" in text
    assert "LOAD FAILED" in text
