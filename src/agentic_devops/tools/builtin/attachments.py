"""`view_image` — re-load a previously attached image by its ref (attachments Phase 3).

Past images are carried through the conversation as a text digest (cheap, so the
model never re-processes the pixels automatically). When the digest isn't enough
— "look more closely at the top-right of that screenshot" — the model calls this
to pull the actual pixels back into view. It returns a ``ToolResult`` with the
image, which the harness renders to the model's vision AND the UI (the same path
a Grafana panel render uses). This is the deliberate, opt-in re-view that keeps
"process once" the default.
"""

from __future__ import annotations

import base64
from typing import Any

from agentic_devops.tools.base import ToolImage, ToolResult, ToolSpec


def build_view_image_tool(blob_store: Any) -> ToolSpec:
    def handler(args: dict[str, Any]) -> Any:
        ref = (args.get("ref") or "").strip()
        if not ref:
            return "ERROR: view_image needs the image 'ref' (the id shown with an earlier attachment)."
        got = blob_store.get(ref)
        if got is None:
            return f"ERROR: no image found for id {ref!r} (it may have expired or the id is wrong)."
        data, mime = got
        return ToolResult(
            text=f"Re-loaded the image (id {ref[:12]}…) so you can look at it directly again.",
            images=[ToolImage(data=base64.b64encode(data).decode(), mime=mime)],
        )

    return ToolSpec(
        name="view_image",
        category="memory",
        description=(
            "Re-load a previously attached image by its id so you can look at the actual "
            "pixels again. Past images in the conversation are represented by a text "
            "description; use this only when that description isn't enough and you need to "
            "SEE the image (read fine detail, a specific region, exact values). The id is "
            "shown in the '[Image the user attached earlier … (id: …)]' note."
        ),
        when_to_use=(
            "re-view a previously attached image; look again at a screenshot/dashboard/"
            "CLI output the user sent earlier; read fine detail the description omitted"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The image id from the earlier-attachment note."}
            },
            "required": ["ref"],
        },
        handler=handler,
        safety_tier="read-only",
    )
