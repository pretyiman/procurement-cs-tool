"""Inline-SVG icon set for the Nocturne-derived UI (see docs/data-model.md
"UI redesign"). Deliberately NOT the Phosphor icon font the original design
reference used (https://unpkg.com/@phosphor-icons/web) - that's an external
CDN dependency, and this app is a local/offline-first desktop tool (same
reasoning CLAUDE.md already applies to HTMX). A small hand-drawn outline set
covering just the icons this UI actually uses keeps the app fully
self-contained with zero network dependency for its own chrome.

Registered as a Jinja global (see main.py: templates.env.globals["icon"] =
icon) so any template can call {{ icon('dashboard') }} directly."""

from markupsafe import Markup

_STROKE = 'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"'

_ICONS = {
    "dashboard": '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="5" rx="1.5"/><rect x="13" y="10" width="8" height="11" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/>',
    "rfqs": '<path d="M6 2.5h9l3 3V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z"/><path d="M15 2.5V6h3"/><path d="M8 12h8M8 15.5h8M8 8.5h4"/>',
    "insights": '<path d="M3 20V4"/><path d="M3 20h18"/><path d="m6.5 16 4-5 3 3 5-7"/>',
    "items": '<path d="M12 2.5 3.5 7 12 11.5 20.5 7 12 2.5Z"/><path d="M3.5 7v10L12 21.5 20.5 17V7"/><path d="M12 11.5V21.5"/>',
    "suppliers": '<path d="M4 21V9l8-5 8 5v12"/><path d="M4 21h16"/><path d="M9.5 21v-6h5v6"/><path d="M9 12h.01M15 12h.01M9 8.5h.01M15 8.5h.01"/>',
    "departments": '<rect x="3" y="9" width="7" height="12" rx="1"/><rect x="14" y="4" width="7" height="17" rx="1"/><path d="M6.5 12.5h0M6.5 15.5h0M6.5 18.5h0M17.5 7.5h0M17.5 10.5h0M17.5 13.5h0M17.5 16.5h0"/>',
    "templates": '<path d="M6 3h9l4 4v13.5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M15 3v4h4"/><path d="M9 21V13.5l2-1.5 2 1.5V21"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 13.5a7.5 7.5 0 0 0 0-3l2-1.4-2-3.4-2.3.9a7.6 7.6 0 0 0-2.6-1.5L14 2h-4l-.5 2.6a7.6 7.6 0 0 0-2.6 1.5l-2.3-.9-2 3.4L4.6 10a7.5 7.5 0 0 0 0 3L2.6 14.9l2 3.4 2.3-.9c.75.66 1.63 1.17 2.6 1.5L10 21.5h4l.5-2.6a7.6 7.6 0 0 0 2.6-1.5l2.3.9 2-3.4-2-1.4Z"/>',
    "collapse": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9.5 4v16"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="1.5"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/><path d="M12 14.5v3"/>',
    "caret-right": '<path d="m9 5 7 7-7 7"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.6-4.6"/>',
    "sun": '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2.5M12 19v2.5M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M2.5 12H5M19 12h2.5M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8"/>',
    "moon": '<path d="M20 14.2A8.5 8.5 0 1 1 9.8 4a7 7 0 0 0 10.2 10.2Z"/>',
    "plus": '<path d="M12 4.5v15M4.5 12h15"/>',
    "arrow-right": '<path d="M4.5 12h15"/><path d="m13 5.5 7 6.5-7 6.5"/>',
    "list-checks": '<path d="m4 6 1.5 1.5L8.5 4.5"/><path d="M13 6h7"/><path d="m4 13 1.5 1.5 3-3"/><path d="M13 13h7"/><path d="M4 20h.01M13 20h7"/>',
    "chart-bar": '<path d="M4 20V10M11 20V4M18 20v-7"/><path d="M2.5 20h19"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.5 2.5L16 9.5"/>',
    "hourglass": '<path d="M6 3h12M6 21h12"/><path d="M7 3c0 4.5 3.5 6.5 5 9 1.5-2.5 5-4.5 5-9M7 21c0-4.5 3.5-6.5 5-9 1.5 2.5 5 4.5 5 9"/>',
    "warning": '<path d="M12 3 2 20.5h20L12 3Z"/><path d="M12 10v4.5M12 18h.01"/>',
    "file-text": '<path d="M6 2.5h9l3 3V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z"/><path d="M15 2.5V6h3"/><path d="M8 12h8M8 15.5h8M8 8.5h4"/>',
    "file-doc": '<path d="M6 2.5h9l3 3V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z"/><path d="M15 2.5V6h3"/><path d="M8 13.5h2.2c1 0 1.8.9 1.8 2s-.8 2-1.8 2H8v-4Zm6 0h1.6c.9 0 1.4.9 1.4 2s-.5 2-1.4 2H14v-4Z"/>',
    "file-xls": '<path d="M6 2.5h9l3 3V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z"/><path d="M15 2.5V6h3"/><path d="m8 13.5 4 4m0-4-4 4"/><path d="M15.5 13.5v4h2.5"/>',
    "file-zip": '<path d="M6 2.5h9l3 3V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z"/><path d="M15 2.5V6h3"/><path d="M11.5 8.5v1.5h1v1.5h-1V13h1v1.5h-1V16a1.5 1.5 0 0 0 3 0"/>',
    "files": '<path d="M8 6.5h8.5L20 10v9.5a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V7.5a1 1 0 0 1 1-1Z"/><path d="M16 6.5V10h4"/><path d="M5 4.5h9a1 1 0 0 1 1 1V6"/>',
    "printer": '<path d="M7 8.5V3h10v5.5"/><rect x="4.5" y="8.5" width="15" height="8" rx="1.5"/><path d="M7 15h10v6H7z"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10.5 5 5 5-5"/><path d="M4.5 19.5h15"/>',
    "upload": '<path d="M12 21V9"/><path d="m7 13.5 5-5 5 5"/><path d="M4.5 19.5h15"/>',
    "edit": '<path d="M4 20h16"/><path d="m6 15.5 9-9 3 3-9 9H6z"/><path d="m14 8 2 2"/>',
    "trash": '<path d="M4.5 6.5h15"/><path d="M9 6.5V4.5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 4.5v2"/><path d="M6.5 6.5 7.3 20a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-13.5"/><path d="M10.2 10.5v7M13.8 10.5v7"/>',
    "scales": '<path d="M12 3v18"/><path d="M6 21h12"/><path d="M4 8h6M14 8h6"/><path d="M7 8 4 14a3 3 0 0 0 6 0L7 8Z"/><path d="M17 8l-3 6a3 3 0 0 0 6 0l-3-6Z"/>',
    "percent": '<path d="m5 19 14-14"/><circle cx="7.5" cy="7.5" r="2.5"/><circle cx="16.5" cy="16.5" r="2.5"/>',
    "value": '<circle cx="12" cy="12" r="9"/><path d="M15 9.5a2.5 2.5 0 0 0-2.5-1.5h-1a2 2 0 0 0 0 4h1a2 2 0 0 1 0 4h-1a2.5 2.5 0 0 1-2.5-1.5"/><path d="M12 6v2.2M12 15.8V18"/>',
    "labels": '<path d="M4 7h5l2-2h9v14H4z"/><path d="M9 7v5l2-1.3L13 12V7"/>',
    "custom-fields": '<path d="M9.5 3 5 8l4.5 5"/><path d="M14.5 3 19 8l-4.5 5"/><path d="M9.5 21 5 16l4.5-5"/><path d="M14.5 21 19 16l-4.5-5"/>',
    "seal-check": '<path d="M12 2.5 14.5 5l3.4-.5.9 3.4 3 1.9-1.5 3.2 1.5 3.2-3 1.9-.9 3.4-3.4-.5L12 23.5 9.5 21l-3.4.5-.9-3.4-3-1.9 1.5-3.2-1.5-3.2 3-1.9.9-3.4 3.4.5Z"/><path d="m8.5 12.5 2.3 2.3L16 9.5"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    "check": '<path d="m4.5 12.5 5 5L20 7"/>',
    "arrow-down": '<path d="M12 4v15"/><path d="m6 13 6 6 6-6"/>',
}


def icon(name: str, size: int = 17, style: str = "") -> Markup:
    """{{ icon('dashboard') }} - renders an inline <svg>. Unknown names
    render nothing rather than raising, so a typo degrades quietly instead
    of crashing document generation."""
    inner = _ICONS.get(name)
    if inner is None:
        return Markup("")
    extra = f' style="{style}"' if style else ""
    return Markup(
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" {_STROKE}{extra}>{inner}</svg>'
    )
