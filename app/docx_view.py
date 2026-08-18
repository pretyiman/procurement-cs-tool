"""Renders an already-generated PP/CA .docx (see docx_export.py) as
printable HTML - a "View & Print" page in the browser instead of
download-then-open-in-Word-then-print.

Deliberately a thin conversion layer over the *exact same* .docx bytes the
download routes produce (same context, same custom-field/department-profile
merge, same docxtpl template) - not a second, hand-authored HTML template.
That means every dynamic value and every custom/shared/department-profile
tag that shows up in the downloaded .docx is guaranteed to show up here
too, with no separate template to keep in sync as templates or custom
fields change.

Self-contained by design (a single function, no new DB/model surface) so
it can be removed cleanly if it turns out not to be useful - see
CLAUDE.md."""

from io import BytesIO

import mammoth


def docx_bytes_to_html(content: bytes) -> str:
    result = mammoth.convert_to_html(BytesIO(content))
    return result.value
