"""The project's legal notice, in one place.

Kept as a module rather than repeated in the README, the API description and
the startup banner, because three copies drift and the one that matters is
whichever the reader happens to hit first.

Wording follows the franklinwh-cloud documentation so the family of projects
says the same thing:
https://david2069.github.io/franklinwh-cloud/

The support clause is an addition. FranklinWH cannot help with this software
and should not be asked to — routing a defect here to their support line wastes
their time and does not get the bug fixed.
"""

from __future__ import annotations

PROJECT_NAME = "franklinwh-modbus-bridge"
ISSUES_URL = "https://github.com/david2069/franklinwh-modbus-bridge/issues"
DOCS_URL = "https://github.com/david2069/franklinwh-modbus-bridge#readme"
CLOUD_DOCS_URL = "https://david2069.github.io/franklinwh-cloud/"
SUNSPEC_URL = "https://sunspec.org/contributing-members/franklin-wh/"
#: The conformance documents themselves, not just the landing page. This is the
#: Modbus project, so the PICS is the spec it implements and the authority
#: docs/vendor-issues.md is written against — a link to a members page makes the
#: reader go hunting for the document that actually settles the question.
PICS_URL = (
    "https://sunspec.org/wp-content/uploads/2009/03/"
    "UPDATED_FranklinWH_Modbus_PICS_SM-000028.xlsx"
)
IEEE_1547_URL = (
    "https://sunspec.org/wp-content/uploads/2009/03/"
    "UPDATED_FranklinWH_Modbus_1547_Certificate_SM-000028.pdf"
)
PICS_DOC_ID = "SM-000028"
TERMS_URL = (
    "https://github.com/david2069/franklinwh-modbus-bridge/blob/main/LICENSE"
)

#: Bump ONLY when the meaning changes, not for a typo. Acknowledgements are
#: stored against this, so bumping re-prompts every user — which is the point
#: when the terms change, and pure noise when they haven't. Consent to wording
#: somebody never read is not consent.
VERSION = "2"

#: The modal's body. A list, not one blob, so the template renders paragraphs
#: without parsing prose — and so this file stays the only place the wording
#: lives.
MODAL_PARAGRAPHS: tuple[str, ...] = (
    "This is an UNOFFICIAL app — NOT affiliated with, or endorsed by, FranklinWH.",
    "It talks to the aGate over SunSpec Modbus, including undocumented vendor "
    "extension registers, which may change, break, or become unavailable "
    "without notice.",
    "It issues WRITE commands to grid-connected battery hardware. Incorrect use "
    "can discharge your battery when you need it, import power when you did not "
    "intend to, or leave the system in an unexpected state.",
    "Provided AS-IS, without warranty of any kind. Use entirely at your own "
    "risk — the authors accept no liability for data loss, equipment damage, or "
    "service loss.",
    "Do NOT contact FranklinWH support about this app. Raise issues, defects, or "
    "feature requests on GitHub instead.",
    "Keep your system compliant with the settings your official FranklinWH app "
    "or installer configured. Those settings may exist to protect battery and "
    "gateway safety, to enforce your local grid profile and any import/export "
    "limits, or to honour incentive, subsidy or VPP programme conditions. Using "
    "this tool to bypass them — deliberately or inadvertently — is entirely at "
    "your own risk, and is neither condoned nor encouraged.",
)

MODAL_TITLE = "Unofficial software"
MODAL_AGREE = "I have read and agree to the above — don't show this again."

#: One line, for places with no room — log prefixes, page footers.
SHORT = (
    "Unofficial software, not endorsed by or affiliated with FranklinWH. "
    "Provided AS IS, without warranty. Do not contact FranklinWH support "
    f"about it — raise issues at {ISSUES_URL}"
)

#: The full notice. Plain text so it reads correctly in a log, a terminal and
#: a docs page without needing three renderings.
FULL = f"""\
UNOFFICIAL SOFTWARE — PLEASE READ

{PROJECT_NAME} is unofficial and is not endorsed, supported, or affiliated
with FranklinWH in any way. FranklinWH and aGate are trademarks of their
respective owners, used here only to describe what this software talks to.

It is provided "AS IS", for educational and informational purposes only,
without warranty of any kind, express or implied, including but not limited to
warranties of merchantability or fitness for any particular purpose. The
authors and contributors accept no responsibility or liability for any
consequences of its use.

By running it you acknowledge that you:
  - are accessing an interface not intended for your use;
  - assume all risk associated with that, including risk to your hardware,
    your warranty, and your electricity supply;
  - will use it responsibly and respect any rate limits;
  - understand that excessive use may affect service for other users.

This software issues WRITE commands to grid-connected battery hardware.
Incorrect use can discharge your battery when you need it, import power when
you did not intend to, or leave the system in an unexpected state.

COMPLIANCE. Keep your system compliant with the settings your official
FranklinWH app or installer configured. Those settings may exist to protect
battery and gateway safety, to enforce your local grid profile and any
import/export limits, or to honour incentive, subsidy or VPP programme
conditions. Using this software to bypass them — deliberately or inadvertently
— is entirely at your own risk, and is neither condoned nor encouraged.

DO NOT CONTACT FRANKLINWH SUPPORT about this software. Bugs, defects and
feature requests belong here, not with the vendor:
  {ISSUES_URL}

Full terms: {TERMS_URL}
SunSpec conformance (PICS {PICS_DOC_ID}): {PICS_URL}
Related project documentation: {CLOUD_DOCS_URL}
"""

#: Markdown for the OpenAPI description, so /docs carries the same notice.
MARKDOWN = f"""\
**Unofficial software.** `{PROJECT_NAME}` is not endorsed, supported, or
affiliated with FranklinWH in any way. Provided **"AS IS"**, for educational
and informational purposes only, without warranty of any kind, express or
implied. The authors and contributors accept no responsibility or liability
for any consequences of its use.

This API issues **write commands to grid-connected battery hardware**. You
assume all risk, including risk to your hardware, your warranty and your
electricity supply.

**Compliance.** Keep your system compliant with the settings your official
FranklinWH app or installer configured — they may enforce battery and gateway
safety, your local grid profile and import/export limits, or incentive/VPP
programme conditions. Using this API to bypass them is entirely at your own
risk and is neither condoned nor encouraged.

**Do not contact FranklinWH support about this software.** Report bugs and
request features at [{ISSUES_URL}]({ISSUES_URL}).

Full terms: [LICENSE]({TERMS_URL}) · SunSpec conformance:
[PICS {PICS_DOC_ID}]({PICS_URL})
"""


def banner() -> str:
    """The full notice framed for a startup log."""
    rule = "=" * 72
    return f"\n{rule}\n{FULL}{rule}"
