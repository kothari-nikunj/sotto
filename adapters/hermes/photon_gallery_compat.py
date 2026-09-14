"""Narrow, fail-closed addition to the pinned Photon sidecar: grouped image delivery."""
from pathlib import Path
import sys

ANCHOR = '    if (req.url === "/send-attachment") {'
PATCH = '''    // Sotto gallery v1: one native multipart message, no separately-sent captions.
    if (req.url === "/gallery-capability") return ok(res, { galleryVersion: 1 });
    if (req.url === "/send-gallery") {
      const { spaceId, paths, summary } = body || {};
      if (!spaceId || typeof summary !== "string" || !summary || summary.length > 1000 || !Array.isArray(paths) || paths.length !== 4 ||
          paths.some(p => typeof p !== "string" || !p.endsWith(".png"))) {
        return badRequest(res, "spaceId and exactly four PNG paths are required");
      }
      const { group, text } = await import("spectrum-ts");
      const space = await resolveSpace(spaceId);
      const result = await space.send(group(text(summary), ...paths.map(p => attachment(p))));
      return ok(res, { messageId: result?.id || null,
        messageIds: result?.content?.items?.map(item => item.id) || [] });
    }
'''


def patch(path):
    path = Path(path)
    text = path.read_text()
    if PATCH in text:
        return
    if text.count(ANCHOR) != 1 or '"/send-gallery"' in text:
        raise RuntimeError('Photon gallery contract changed; review the runtime pin')
    path.write_text(text.replace(ANCHOR, PATCH + ANCHOR))


if __name__ == '__main__':
    patch(sys.argv[1])
